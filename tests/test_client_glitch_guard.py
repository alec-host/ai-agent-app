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
async def test_client_save_failure_retains_session(monkeypatch):
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
    
    # Mock MatterMinerCoreClient.create_client to return an error dict
    mock_core_client = MagicMock()
    mock_core_client.create_client = AsyncMock(return_value={"status": "error", "message": "Not Found"})
    mock_core_client.set_auth_token = MagicMock()
    
    monkeypatch.setattr("src.agents.client_creation_agent.MatterMinerCoreClient", lambda **kwargs: mock_core_client)
    
    services = {"calendar": mock_cal_service}
    
    result = await handle_client_creation("create_client_record", {}, services, "12345678", [])
    
    assert result["status"] == "error"
    assert "Not Found" in result["message"]
    
    # CRITICAL: clear_client_session should NOT be called
    mock_cal_service.clear_client_session.assert_not_called()

@pytest.mark.asyncio
async def test_client_save_success_clears_session(monkeypatch):
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
    
    # Mock MatterMinerCoreClient.create_client to return success
    mock_core_client = MagicMock()
    mock_core_client.create_client = AsyncMock(return_value={"status": "success"})
    mock_core_client.set_auth_token = MagicMock()
    
    monkeypatch.setattr("src.agents.client_creation_agent.MatterMinerCoreClient", lambda **kwargs: mock_core_client)
    
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
    Verify that create_client hits the correct remote endpoint.
    """
    from src.remote_services.matterminer_core import MatterMinerCoreClient
    
    client = MatterMinerCoreClient(
        base_url="https://dev.matterminer.com/api",
        tenant_id="12345678"
    )
    client.set_auth_token("test_token")
    
    # Mock the Remote Core endpoint
    route = respx.post("https://dev.matterminer.com/api/clients").mock(
        return_value=httpx.Response(201, json={"status": "success"})
    )
    
    client_data = {"first_name": "John", "last_name": "Doe"}
    resp = await client.create_client(client_data)
    
    assert route.called
    assert resp["status"] == "success"
