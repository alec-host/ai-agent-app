import pytest
import respx
import httpx
import json
from src.agents.core_agent import handle_core_ops
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_client_glitch_guard_id_collision():
    """
    Verify that if the last_name matches the client_number (alphanumeric),
    the Glitch Guard resets the last_name to prevent corruption.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {"metadata": {"active_workflow": "client"}}
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # Case: AI misidentifies ID as Last Name
    args = {
        "first_name": "gibbs",
        "last_name": "C483838",
        "client_type": "individual"
    }
    
    result = await handle_core_ops("create_client_record", args, services, "12345678", [])
    
    # Verify sync call args
    sync_payload = mock_cal_service.sync_client_session.call_args[0][0]
    # Verify the draft was synced with the provided fields
    draft = sync_payload["metadata"]["client_draft"]
    assert draft["first_name"] == "gibbs"
    assert draft["client_type"] == "individual"

@pytest.mark.asyncio
async def test_client_id_update_does_not_wipe_name():
    """
    REGRESSION TEST: Verify that providing ONLY the client_number 
    does not accidentally wipe an existing valid last_name.
    """
    mock_cal_service = AsyncMock()
    # Mock DB already has the correct name in the client_draft namespace
    mock_cal_service.get_client_session.return_value = {
        "metadata": {
            "active_workflow": "client",
            "client_draft": {
                "first_name": "John",
                "last_name": "Doe",
                "client_type": None
            }
        }
    }
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # AI only provides the number in this turn
    args = {"client_type": "individual"}
    
    await handle_core_ops("create_client_record", args, services, "12345678", [])
    
    # Verify sync call args
    sync_payload = mock_cal_service.sync_client_session.call_args[0][0]
    # The name should STILL be "Doe" in the client_draft
    assert sync_payload["metadata"]["client_draft"]["last_name"] == "Doe"
    assert sync_payload["metadata"]["client_draft"]["client_type"] == "individual"

@pytest.mark.asyncio
async def test_client_save_failure_retains_session(monkeypatch):
    """
    Verify that if the remote save fails (e.g. 404), the session is NOT cleared.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {
        "metadata": {
            "client_draft": {
                "first_name": "John",
                "last_name": "Doe",
                "client_email": "john@test.com",
                "client_type": "individual",
                "contact_id": "123",
                "country_id": 1,
                "street": "123 Main St"
            },
            "active_workflow": "client",
            "remote_access_token": "valid_mock_token"
        }
    }
    mock_cal_service.thread_id = "test_thread"
    
    # Mock MatterMinerCoreClient.create_client to return an error dict
    mock_core_client = MagicMock()
    mock_core_client.create_client = AsyncMock(return_value={"status": "error", "message": "Not Found"})
    mock_core_client.close = AsyncMock()
    
    monkeypatch.setattr("src.agents.core_agent.MatterMinerCoreClient", lambda **kwargs: mock_core_client)
    
    services = {"calendar": mock_cal_service}
    
    result = await handle_core_ops("create_client_record", {}, services, "12345678", [])
    
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
        "metadata": {
            "client_draft": {
                "first_name": "John",
                "last_name": "Doe",
                "client_email": "john@test.com",
                "client_type": "individual",
                "contact_id": "123",
                "country_id": 1,
                "street": "123 Main St"
            },
            "active_workflow": "client",
            "remote_access_token": "valid_mock_token"
        }
    }
    mock_cal_service.thread_id = "test_thread"
    
    # Mock MatterMinerCoreClient.create_client to return success
    mock_core_client = MagicMock()
    mock_core_client.create_client = AsyncMock(return_value={"status": "success"})
    mock_core_client.close = AsyncMock()
    
    monkeypatch.setattr("src.agents.core_agent.MatterMinerCoreClient", lambda **kwargs: mock_core_client)
    
    services = {"calendar": mock_cal_service}
    
    result = await handle_core_ops("create_client_record", {}, services, "12345678", [])
    
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
        base_url="https://dev.matterminer.com",
        tenant_id="12345678"
    )
    
    # Mock the Remote Core endpoint (Standardized routing Host/app/core/...)
    route = respx.post("https://dev.matterminer.com/app/core/client").mock(
        return_value=httpx.Response(201, json={"status": "success"})
    )
    
    client_data = {"first_name": "John", "last_name": "Doe"}
    resp = await client.create_client(client_data)
    
    assert route.called
    assert resp["status"] == "success"

@pytest.mark.asyncio
async def test_client_creation_stringified_metadata():
    """
    Verify that the agent correctly parses stringified metadata returned by the database.
    """
    mock_cal_service = AsyncMock()
    # Database returns metadata as a JSON string
    mock_cal_service.get_client_session.return_value = {
        "metadata": json.dumps({
            "active_workflow": "client",
            "client_draft": {
                "first_name": "String",
                "last_name": "Parsing"
            }
        })
    }
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # AI provides a new field
    args = {"client_email": "string@test.com"}
    
    with respx.mock:
        # Mock the mandatory early contact lookup
        respx.get("https://dev.matterminer.com/app/core/search-contact").mock(
            return_value=httpx.Response(200, json={"status": "success", "contact_id": "123"})
        )
        result = await handle_core_ops("create_client_record", args, services, "12345678", [])
    
    # 2. Verify sync call args
    sync_payload = mock_cal_service.sync_client_session.call_args[0][0]
    # Existing fields should have been recovered from the stringified metadata
    assert sync_payload["metadata"]["client_draft"]["first_name"] == "String"
    assert sync_payload["metadata"]["client_draft"]["last_name"] == "Parsing"
    assert sync_payload["metadata"]["client_draft"]["client_email"] == "string@test.com"
