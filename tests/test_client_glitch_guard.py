import pytest
import respx
import httpx
import json
from src.agents.client_creation_agent import handle_client_creation
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_client_glitch_guard_id_collision():
    """
    Verify that if the last_name matches the client_number (alphanumeric),
    the Glitch Guard resets the last_name to prevent corruption.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {"metadata": {}}
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # Case: AI misidentifies ID as Last Name
    args = {
        "first_name": "gibbs",
        "last_name": "C483838",
        "client_number": "C483838"
    }
    
    result = await handle_client_creation("create_client_record", args, services, "12345678", [])
    
    # Verify sync call args
    sync_payload = mock_cal_service.sync_client_session.call_args[0][0]
    # Glitch guard should have reset last_name to None because it matched the alphanumeric ID
    assert sync_payload["last_name"] is None
    assert sync_payload["client_number"] == "C483838"

@pytest.mark.asyncio
async def test_client_id_update_does_not_wipe_name():
    """
    REGRESSION TEST: Verify that providing ONLY the client_number 
    does not accidentally wipe an existing valid last_name.
    """
    mock_cal_service = AsyncMock()
    # Mock DB already has the correct name
    mock_cal_service.get_client_session.return_value = {
        "first_name": "John",
        "last_name": "Doe",
        "client_number": None
    }
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # AI only provides the number in this turn
    args = {"client_number": "C483838"}
    
    await handle_client_creation("create_client_record", args, services, "12345678", [])
    
    # Verify sync call args
    sync_payload = mock_cal_service.sync_client_session.call_args[0][0]
    # The name should STILL be "Doe"
    assert sync_payload["last_name"] == "Doe"
    assert sync_payload["client_number"] == "C483838"

@pytest.mark.asyncio
async def test_client_save_failure_retains_session():
    """
    Verify that if the remote save fails (e.g. 404), the session is NOT cleared.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {
        "first_name": "John",
        "last_name": "Doe",
        "client_number": "C123",
        "client_type": "individual",
        "email": "john@test.com",
        "metadata": {
            "active_workflow": "client",
            "remote_access_token": "valid_mock_token"
        }
    }
    mock_cal_service.thread_id = "test_thread"
    
    # Mock save_new_client to return a 404 response
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.text = "Not Found"
    mock_cal_service.save_new_client.return_value = mock_response
    
    services = {"calendar": mock_cal_service}
    
    result = await handle_client_creation("create_client_record", {}, services, "12345678", [])
    
    assert result["status"] == "error"
    assert "Not Found" in result["message"]
    
    # CRITICAL: clear_client_session should NOT be called
    mock_cal_service.clear_client_session.assert_not_called()

@pytest.mark.asyncio
async def test_client_save_success_clears_session():
    """
    Verify that if the remote save succeeds, the session is cleared.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {
        "first_name": "John",
        "last_name": "Doe",
        "client_number": "C123",
        "client_type": "individual",
        "email": "john@test.com",
        "metadata": {
            "active_workflow": "client",
            "remote_access_token": "valid_mock_token"
        }
    }
    mock_cal_service.thread_id = "test_thread"
    
    # Mock save_new_client to return a 201 Created response
    mock_response = MagicMock()
    mock_response.status_code = 201
    mock_cal_service.save_new_client.return_value = mock_response
    
    services = {"calendar": mock_cal_service}
    
    result = await handle_client_creation("create_client_record", {}, services, "12345678", [])
    
    assert result["status"] == "success"
    
    # Verify session management
    assert mock_cal_service.sync_client_session.called
    mock_cal_service.clear_client_session.assert_called_once_with("12345678")

@pytest.mark.asyncio
@respx.mock
async def test_save_new_client_endpoint_resolution():
    """
    Verify that save_new_client uses the authenticated request wrapper correctly.
    """
    from src.main import CalendarServiceClient
    
    client = CalendarServiceClient(
        tenant_id="12345678", 
        http_client=httpx.AsyncClient(), 
        correlation_id="test_corr"
    )
    
    # Mock the Remote Core endpoint
    # Note: save_new_client now uses node_remote_service_url (https://dev.matterminer.com/api)
    route = respx.post("https://dev.matterminer.com/api/clients").mock(
        return_value=httpx.Response(201, json={"status": "success"})
    )
    
    client_data = {"first_name": "John", "last_name": "Doe"}
    resp = await client.save_new_client(client_data, "12345678", token="mock_remote_token")
    
    assert route.called
    assert resp["status"] == "success"
