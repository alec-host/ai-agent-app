import pytest
import respx
import json
from httpx import Response
from src.agents.core_agent import handle_create_client
from src.config import settings

@pytest.mark.asyncio
async def test_client_creation_reactive_auth():
    """
    Test that handle_create_client triggers auth_required correctly
    when the remote service returns a 404 'Not found' signal.
    """
    tenant_id = "test-tenant-client"
    
    # 1. Setup Mock for 404 Auth Trigger
    with respx.mock:
        respx.post(f"{settings.NODE_REMOTE_SERVICE_URL}/client").mock(return_value=Response(
            404, 
            json={"success": False, "message": "Not found"}
        ))
        
        # 2. Mock services with a full client draft
        async def mock_get_session(t):
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

        mock_services = {
            'calendar': type('obj', (object,), {
                'get_client_session': mock_get_session,
                'sync_client_session': mock_sync_session,
                'access_token': None,
                'thread_id': "test-thread-client"
            })
        }
        
        args = {} # All fields already in draft/session
        result = await handle_create_client(args, mock_services, tenant_id, history=[])
        
        # 3. Assertions
        assert result["status"] == "auth_required"
        assert result["auth_type"] == "matterminer_core"
        assert "login card" in result["response_instruction"]

@pytest.mark.asyncio
async def test_client_creation_unexpected_error():
    """
    Verify that non-404 errors are handled as errors, not auth triggers.
    """
    tenant_id = "test-tenant-error"
    
    with respx.mock:
        respx.post(f"{settings.NODE_REMOTE_SERVICE_URL}/client").mock(return_value=Response(
            500, 
            json={"success": False, "message": "Database error"}
        ))
        
        async def mock_get_session(t):
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

        mock_services = {
            'calendar': type('obj', (object,), {
                'get_client_session': mock_get_session,
                'sync_client_session': mock_sync_session,
                'access_token': None,
                'thread_id': "test-thread-error"
            })
        }
        
        result = await handle_create_client({}, mock_services, tenant_id, history=[])
        
        assert result["status"] == "error"
        assert "rejected the record" in result["message"]
        assert "Database error" in result["message"]
