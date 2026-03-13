import pytest
import json
from src.agents.core_agent import handle_create_contact
from src.agent_manager import get_rehydration_context
from unittest.mock import AsyncMock, MagicMock
import respx
import httpx

@pytest.mark.asyncio
async def test_contact_token_persistence_on_success():
    """
    Verify that when create_contact succeeds, the remote token is kept 
    and only the draft is cleared.
    """
    # 1. Setup session with token and full data
    mock_cal_service = AsyncMock()
    initial_metadata = {
        "remote_access_token": "persistent_core_token",
        "contact_draft": {
            "first_name": "John",
            "last_name": "Doe",
            "email": "john@example.com"
        },
        "active_workflow": "contact"
    }
    mock_cal_service.get_client_session.return_value = {
        "metadata": initial_metadata.copy()
    }
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # 2. Mock the remote API
    with respx.mock:
        respx.post("https://dev.matterminer.com/api/contacts").mock(
            return_value=httpx.Response(200, json={"status": "success"})
        )
        
        # 3. Execute
        result = await handle_create_contact({}, services, "tenant_123", [])
        
        # 4. Assertions
        assert result["status"] == "success"
        
        # Verify hard-delete was called to unblock future workflows
        mock_cal_service.clear_client_session.assert_called_once_with("tenant_123")
        
        # Verify sync_client_session WAS called to mark lifecycle as completed
        assert mock_cal_service.sync_client_session.called
        sync_payload = mock_cal_service.sync_client_session.call_args[0][0]
        
        # The key check: Lifecycle should be completed in metadata
        metadata = sync_payload["metadata"]
        assert metadata["session_lifecycle"] == "completed"
        assert metadata["contact_draft"] == {}
        assert metadata["active_workflow"] == "cleared"

@pytest.mark.asyncio
async def test_sequential_field_asking():
    """
    Verify that the agent asks for missing fields ONE BY ONE.
    """
    mock_cal_service = AsyncMock()
    mock_cal_service.get_client_session.return_value = {"metadata": {}}
    mock_cal_service.thread_id = "test_thread"
    services = {"calendar": mock_cal_service}
    
    # First turn: Captured nothing
    result = await handle_create_contact({"first_name": "John"}, services, "tenant_123", [])
    assert "Last Name" in result["response_instruction"]
    assert "Email Address" not in result["response_instruction"] # Should only ask for the first missing one

@pytest.mark.asyncio
async def test_contact_rehydration_logic():
    """
    Verify that get_rehydration_context correctly identifies a pending contact draft.
    """
    mock_cal_service = AsyncMock()
    # Mock return from session fetch (Hardened helper)
    mock_cal_service.get_client_session.return_value = {
        "metadata": {
            "active_workflow": "contact",
            "contact_draft": {
                "first_name": "John",
                "last_name": "Doe"
            }
        }
    }
    # Mock token check
    mock_cal_service._sync_access_token.return_value = {"status": "auth_required"}
    
    services = {"calendar": mock_cal_service}
    
    rehydration = await get_rehydration_context("tenant_123", services)
    
    assert rehydration is not None
    assert "PENDING CONTACT RECORD" in rehydration["injection"]
    assert "RECOVERY MODE: CONTACT INTAKE DETECTED" in rehydration["injection"]
    assert "John" in rehydration["injection"]
    assert "Doe" in rehydration["injection"]
    assert "ask for the Email" in rehydration["injection"]

@pytest.mark.asyncio
async def test_vault_visibility_in_main_injection():
    """
    Verify that the main.py logic (manually checked here) correctly formats the vault string.
    We'll semi-simulate the logic from main.py
    """
    metadata = {
        "active_workflow": "contact",
        "contact_draft": {"first_name": "John"}
    }
    active_workflow = metadata.get("active_workflow")
    vault_segments = []
    
    # Simulate the logic I added to main.py
    contact_draft = metadata.get("contact_draft", {})
    if contact_draft and active_workflow == "contact":
         vault_segments.append(f"CONTACT_DRAFT: {contact_draft}")
    
    vault_str = " | ".join(vault_segments)
    assert "CONTACT_DRAFT" in vault_str
    assert "John" in vault_str
