import pytest
import respx
import httpx
import json
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.agents.core_agent import handle_core_ops, handle_create_contact, handle_lookup_countries
from unittest.mock import AsyncMock, MagicMock

# Mock Data from User Specs
LOGIN_RESPONSE = {
    "data": {
        "token": "mock_core_token",
        "user": {
            "id": 4,
            "full_name": "Dev User",
            "email": "dev@matterminer.com",
            "tenant_id": "12345678"
        }
    },
    "status": "success",
    "message": "Login successful"
}

COUNTRY_RESPONSE = {
    "status": "success",
    "data": [
        {"id": 4, "name": "Kenya"},
        {"id": 5, "name": "Uganda"}
    ]
}

CONTACT_SUCCESS_RESPONSE = {
    "status": "success",
    "message": "Contact created successfully"
}

@pytest.mark.asyncio
@respx.mock
async def test_core_client_login():
    # Setup mock
    respx.post(url__regex=r".*/login.*").mock(
        return_value=httpx.Response(200, json=LOGIN_RESPONSE)
    )
    
    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    resp = await client.login("dev@matterminer.com", "password")
    
    assert resp["status"] == "success"
    assert client.access_token == "mock_core_token"
    assert client.user_profile["full_name"] == "Dev User"
    await client.close()

@pytest.mark.asyncio
@respx.mock
async def test_core_client_get_countries():
    # Setup mock
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(200, json=COUNTRY_RESPONSE)
    )
    
    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    client.set_auth_token("test_token")
    resp = await client.get_countries(search="Kenya")
    
    assert resp["status"] == "success"
    assert len(resp["data"]) == 2
    assert resp["data"][0]["name"] == "Kenya"
    await client.close()

@pytest.mark.asyncio
async def test_agent_handle_create_contact_drafting():
    # Mock services
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {"metadata": {}}
    mock_cal_service.thread_id = "test_thread"
    
    services = {"calendar": mock_cal_service}
    args = {"first_name": "Jane", "last_name": "Smith"} # Missing Email
    
    result = await handle_create_contact(args, services, "12345678", [])
    
    # Assertions
    assert result["status"] == "partial_success"
    assert "Email Address" in result["response_instruction"]
    
    # Verify sync was called with the draft
    sync_call_args = mock_cal_service.sync_client_session.call_args[0][0]
    assert sync_call_args["metadata"]["contact_draft"]["first_name"] == "Jane"
    assert sync_call_args["metadata"]["active_workflow"] == "contact"

@pytest.mark.asyncio
async def test_agent_handle_create_contact_drafting_with_partial_data():
    """
    Contact creation is conversational. Providing some fields returns partial_success
    and asks for the next missing field.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {"metadata": {}}
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # Provide only first_name — many fields still missing
    result = await handle_create_contact({"first_name": "Jane"}, services, "12345678", [])
    assert result["status"] == "partial_success"
    assert "Last Name" in result["response_instruction"]

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_create_contact_finalize():
    """
    When ALL required fields are present in the draft, the agent finalizes
    by POSTing to the remote API.
    """
    # Provide a complete draft with all required CONTACT_SCHEMA fields
    complete_draft = {
        "first_name": "Jane",
        "last_name": "Smith",
        "client_email": "jane@example.com",
        "contact_type": "primary",
        "title": "Ms.",
        "middle_name": "A",
        "country_code": "+254",
        "phone_number": "712345678"
    }
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {
        "metadata": {
            "active_workflow": "contact",
            "contact_draft": complete_draft
        }
    }
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}

    # Mock the remote API
    respx.post(url__regex=r".*/contact.*").mock(
        return_value=httpx.Response(200, json=CONTACT_SUCCESS_RESPONSE)
    )

    result = await handle_create_contact({}, services, "12345678", [])
    assert result["status"] == "success"
    mock_cal_service.sync_client_session.assert_called()
    mock_cal_service.clear_client_session.assert_called_once_with("12345678")

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_lookup_countries():
    # Setup session with token
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {
        "metadata": {"remote_access_token": "valid_token"}
    }
    services = {"calendar": mock_cal_service}
    
    # Mock the remote API
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(200, json=COUNTRY_RESPONSE)
    )
    
    result = await handle_lookup_countries({"search": "Kenya"}, services, "12345678")
    assert result["status"] == "success"
    assert "Kenya (ID: 4)" in result["countries"]

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_create_contact_failure():
    """
    When the remote API returns 500, the agent should return an error.
    Requires a COMPLETE draft so the agent actually attempts the POST.
    """
    complete_draft = {
        "first_name": "Jane",
        "last_name": "Smith",
        "client_email": "fail@test.com",
        "contact_type": "primary",
        "title": "Ms.",
        "middle_name": "A",
        "country_code": "+254",
        "phone_number": "712345678"
    }
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {
        "metadata": {
            "remote_access_token": "valid_token",
            "active_workflow": "contact",
            "contact_draft": complete_draft
        }
    }
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}

    respx.post(url__regex=r".*/contact.*").mock(
        return_value=httpx.Response(500, json={"message": "Internal Server Error"})
    )

    result = await handle_create_contact({}, services, "12345678", [])
    assert result["status"] == "error"
    assert "Internal Server Error" in result["message"]
