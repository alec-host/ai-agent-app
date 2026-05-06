import pytest
import respx
import json
from httpx import Response
from src.agents.core_agent import handle_create_client
from src.config import settings

@pytest.mark.asyncio
async def test_client_creation_api_key_rejection():
    """
    Phase 6 (Auth Migration): Test that handle_create_client triggers api_key_error
    when the remote service returns 401 (API key rejected).
    Previously tested 404 → auth_required; now tests 401 → api_key_error.
    """
    tenant_id = "test-tenant-client"
    
    # 1. Setup Mock for 401 API Key Rejection
    with respx.mock:
        respx.post(url__regex=r".*/client.*").mock(return_value=Response(
            401, 
            json={"success": False, "message": "Unauthorized"}
        ))
        
        # 2. Mock services with a full client draft
        async def mock_get_session(t, user_email=None):
            return {
                "metadata": {
                    "active_workflow": "client",
                    "client_draft": {
                        "client_type": "individual",
                        "client_email": "client@example.com",
                        "first_name": "Alice",
                        "last_name": "Smith",
                        "contact_id": "cont-123",
                        "country_id": 1,
                        "street": "123 Main St"
                    }
                }
            }
            
        async def mock_sync_session(p):
            return True

        from unittest.mock import AsyncMock
        mock_calendar = AsyncMock()
        mock_calendar.get_client_session = mock_get_session
        mock_calendar.sync_client_session = mock_sync_session
        mock_calendar.clear_client_session = AsyncMock(return_value=True)
        mock_calendar.access_token = None
        mock_calendar.thread_id = "test-thread-client"
        mock_services = {'calendar': mock_calendar, 'session': mock_calendar}
        
        args = {} # All fields already in draft/session
        result = await handle_create_client(args, mock_services, tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email="test@example.com")
        
        # 3. Assertions — api_key_error, not auth_required
        assert result["status"] == "api_key_error"
        assert result["auth_type"] == "matterminer_core"
        assert "configuration" in result["response_instruction"].lower() or \
               "administrator" in result["response_instruction"].lower()
        # Must NOT reference login card
        assert "login card" not in result.get("response_instruction", "").lower()

@pytest.mark.asyncio
async def test_client_creation_404_is_data_error():
    """
    Phase 6 (Auth Migration): 404 from Core should be treated as a normal
    data error, NOT as an auth failure.
    """
    tenant_id = "test-tenant-client-404"
    
    with respx.mock:
        respx.post(url__regex=r".*/client.*").mock(return_value=Response(
            404, 
            json={"success": False, "message": "Not found"}
        ))
        
        async def mock_get_session(t, user_email=None):
            return {
                "metadata": {
                    "active_workflow": "client",
                    "client_draft": {
                        "client_type": "individual",
                        "client_email": "client@example.com",
                        "first_name": "Alice",
                        "last_name": "Smith",
                        "contact_id": "cont-123",
                        "country_id": 1,
                        "street": "123 Main St"
                    }
                }
            }
            
        async def mock_sync_session(p):
            return True

        from unittest.mock import AsyncMock
        mock_calendar = AsyncMock()
        mock_calendar.get_client_session = mock_get_session
        mock_calendar.sync_client_session = mock_sync_session
        mock_calendar.clear_client_session = AsyncMock(return_value=True)
        mock_calendar.access_token = None
        mock_calendar.thread_id = "test-thread-client-404"
        mock_services = {'calendar': mock_calendar, 'session': mock_calendar}
        
        result = await handle_create_client({}, mock_services, tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email="test@example.com")
        
        # 404 should be a normal error, NOT api_key_error or auth_required
        assert result["status"] == "error", f"404 should be a normal error, got: {result['status']}"
        assert result.get("status") != "api_key_error"
        assert result.get("status") != "auth_required"

@pytest.mark.asyncio
async def test_client_creation_unexpected_error():
    """
    Verify that non-auth errors (500) are handled as errors, not auth triggers.
    """
    tenant_id = "test-tenant-error"
    
    with respx.mock:
        respx.post(url__regex=r".*/client.*").mock(return_value=Response(
            500, 
            json={"success": False, "message": "Database error"}
        ))
        
        async def mock_get_session(t, user_email=None):
            return {
                "metadata": {
                    "active_workflow": "client",
                    "client_draft": {
                        "client_type": "individual",
                        "client_email": "error@example.com",
                        "first_name": "Bob",
                        "last_name": "Ross",
                        "contact_id": "cont-456",
                        "country_id": 2,
                        "street": "456 Oak Rd"
                    }
                }
            }
            
        async def mock_sync_session(p):
            return True

        from unittest.mock import AsyncMock
        mock_calendar = AsyncMock()
        mock_calendar.get_client_session = mock_get_session
        mock_calendar.sync_client_session = mock_sync_session
        mock_calendar.clear_client_session = AsyncMock(return_value=True)
        mock_calendar.access_token = None
        mock_calendar.thread_id = "test-thread-error"
        mock_services = {'calendar': mock_calendar, 'session': mock_calendar}
        
        result = await handle_create_client({}, mock_services, tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email="test@example.com")
        
        assert result["status"] == "error"
        assert "Database error" in result["message"] or "Failed to create client" in result["message"]
