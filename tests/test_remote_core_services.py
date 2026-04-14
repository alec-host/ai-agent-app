import pytest
import respx
import httpx
import json
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.agents.core_agent import handle_core_ops, handle_create_contact, handle_lookup_countries
from src.config import settings
from unittest.mock import AsyncMock, MagicMock

# Phase 6 (Auth Migration): LOGIN_RESPONSE removed — login() method no longer exists.

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

# ============================================================
# Phase 6 NEW: API Key Header Verification
# ============================================================

@pytest.mark.asyncio
@respx.mock
async def test_api_key_header_injected():
    """Every Core request must include Authorization: Bearer {CORE_API_KEY}."""
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(200, json=COUNTRY_RESPONSE)
    )

    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    resp = await client.get_countries(search="Kenya")

    assert resp["status"] == "success"

    # Verify the outbound request contained the API key
    assert len(respx.calls) == 1
    sent_headers = dict(respx.calls[0].request.headers)
    assert "authorization" in sent_headers
    assert sent_headers["authorization"] == f"Bearer {settings.CORE_API_KEY}"
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_api_key_header_with_user_email():
    """API key header + user email should both be present."""
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(200, json=COUNTRY_RESPONSE)
    )

    client = MatterMinerCoreClient(
        base_url="https://dev.matterminer.com/api",
        tenant_id="12345678",
        user_email="dev@matterminer.com"
    )
    resp = await client.get_countries(search="Uganda")

    sent_headers = dict(respx.calls[0].request.headers)
    assert sent_headers["authorization"] == f"Bearer {settings.CORE_API_KEY}"
    assert sent_headers["x-user-email"] == "dev@matterminer.com"
    assert sent_headers["x-tenant-id"] == "12345678"
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_login_method_removed():
    """MatterMinerCoreClient must no longer have a login() method."""
    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    assert not hasattr(client, 'login'), "login() method should have been removed in Phase 1"
    assert not hasattr(client, 'has_valid_token'), "has_valid_token() should have been removed"
    assert not hasattr(client, 'set_auth_token'), "set_auth_token() should have been removed"
    assert not hasattr(client, 'access_token'), "access_token attribute should have been removed"
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_401_returns_api_key_error():
    """Core 401 → api_key_error status, not auth_required."""
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )

    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    resp = await client.get_countries(search="test")

    assert resp["status"] == "api_key_error"
    assert resp["code"] == 401
    assert "administrator" in resp["message"].lower()
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_403_returns_api_key_error():
    """Core 403 → api_key_error status, not auth_required."""
    respx.post(url__regex=r".*/contact.*").mock(
        return_value=httpx.Response(403, json={"message": "Forbidden"})
    )

    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    resp = await client.create_contact({"first_name": "Test"})

    assert resp["status"] == "api_key_error"
    assert resp["code"] == 403
    await client.close()


@pytest.mark.asyncio
@respx.mock
async def test_404_is_data_error():
    """Core 404 → normal error status, NOT api_key_error or auth_required."""
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(404, json={"message": "Not found"})
    )

    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    resp = await client.get_countries(search="nonexistent")

    assert resp["status"] == "error"
    assert resp.get("status") != "api_key_error"
    assert resp.get("status") != "auth_required"
    await client.close()


# ============================================================
# Existing Tests (Updated for Phase 6)
# ============================================================

@pytest.mark.asyncio
@respx.mock
async def test_core_client_get_countries():
    """Countries endpoint works with static API key — no manual auth needed."""
    respx.get(url__regex=r".*/countries.*").mock(
        return_value=httpx.Response(200, json=COUNTRY_RESPONSE)
    )

    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com/api", tenant_id="12345678")
    # Phase 6: No set_auth_token() call needed — API key is automatic
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
