import pytest
import json
import respx
from httpx import Response
from src.agents.core_agent import handle_create_client
from src.config import settings
from unittest.mock import AsyncMock

@pytest.mark.asyncio
async def test_client_to_contact_cross_pollination():
    """
    Test that when a contact is not found during client creation, 
    the system correctly pre-fills the contact draft with collected names.
    """
    tenant_id = "test-cross-pollinate"
    user_email = "test@example.com"
    
    # 1. Setup Mock for 404 Search
    with respx.mock:
        respx.get(url__regex=r".*/search-contact.*").mock(return_value=Response(
            404, 
            json={"success": False, "message": "Contact not found"}
        ))
        
        # 2. Mock services with a client draft
        
        # We need a mutable reference for sync results
        sync_recorded = []
        
        async def mock_get_session(t, user_email=None):
            return {
                "metadata": {
                    "active_workflow": "client",
                    "client_draft": {
                        "first_name": "Juma",
                        "last_name": "Kandie",
                        "client_email": "juma.kandie@yapmail.com",
                        "client_type": "individual"
                    }
                }
            }
        
        async def mock_sync_session(p):
            sync_recorded.append(p)
            return True

        async def mock_clear_session(t):
            return True

        mock_calendar = AsyncMock()
        mock_calendar.get_client_session = mock_get_session
        mock_calendar.sync_client_session = mock_sync_session
        mock_calendar.clear_client_session = AsyncMock(return_value=True)
        mock_calendar.access_token = "test-token"
        mock_calendar.thread_id = "test-thread-cross"

        mock_services = {
            'calendar': mock_calendar, 'session': mock_calendar
        }
        
        # 3. Test handle_create_client
        # Initial call where email and names are provided but no contact_id
        args = {
            "first_name": "Juma",
            "last_name": "Kandie",
            "client_email": "juma.kandie@yapmail.com",
            "client_type": "individual"
        }
        
        # The core_agent uses _get_core_client which needs proper mocking for unit tests, 
        # but since it uses MatterMinerCoreClient internally and that uses self.request, 
        # respx.mock should capture it if we mock the URL correctly.
        
        result = await handle_create_client(args, mock_services, tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email=user_email)
        
        # 4. Assertions
        assert result["status"] == "partial_success"
        assert result["next_target"] == "contact_id"
        assert "contact record is required" in result["response_instruction"]
        
        # Verify sync_payload contains the contact_draft
        assert len(sync_recorded) > 0
        last_payload = sync_recorded[-1]
        metadata = last_payload.get("metadata", {})
        
        contact_draft = metadata.get("contact_draft", {})
        assert contact_draft.get("first_name") == "Juma"
        assert contact_draft.get("last_name") == "Kandie"
        assert contact_draft.get("client_email") == "juma.kandie@yapmail.com"
        assert metadata.get("_must_create_contact") is True

if __name__ == "__main__":
    pytest.main([__file__])
