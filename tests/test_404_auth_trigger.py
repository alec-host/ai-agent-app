import pytest
import respx
from httpx import Response
from src.agents.core_agent import handle_create_contact, handle_lookup_countries
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.config import settings

@pytest.mark.asyncio
async def test_404_auth_trigger_lookup_countries():
    """Verify that a 404 Not Found from the remote service triggers auth_required."""
    tenant_id = "test-tenant"
    
    # Mock the 404 Not Found response
    with respx.mock:
        respx.get(url__regex=r".*/countries.*").mock(return_value=Response(
            404, 
            json={"success": False, "message": "Not found"}
        ))
        
        # Mock services
        async def mock_get_session(t):
            return {"metadata": {}}

        mock_services = {
            'calendar': type('obj', (object,), {
                'get_client_session': mock_get_session,
                'access_token': None,
                'thread_id': "test-thread"
            })
        }
        
        args = {"search": "test"}
        result = await handle_lookup_countries(args, mock_services, tenant_id)
        
        assert result["status"] == "auth_required"
        assert "login card" in result["response_instruction"]

@pytest.mark.asyncio
async def test_404_auth_trigger_create_contact():
    """Verify that a 404 Not Found during contact creation triggers auth_required."""
    tenant_id = "test-tenant"
    
    with respx.mock:
        respx.post(url__regex=r".*/contact.*").mock(return_value=Response(
            404, 
            json={"success": False, "message": "Not found"}
        ))
        
        # Full draft to skip partial success
        async def mock_get_session(t):
            return {
                "metadata": {
                    "active_workflow": "contact",
                    "contact_draft": {
                        "first_name": "John",
                        "last_name": "Doe",
                        "client_email": "john@doe.com",
                        "contact_type": "primary",
                        "title": "Mr.",
                        "middle_name": "James",
                        "country_code": "US",
                        "phone_number": "1234567890"
                    }
                }
            }
            
        async def mock_sync_session(p):
            return None

        mock_services = {
            'calendar': type('obj', (object,), {
                'get_client_session': mock_get_session,
                'sync_client_session': mock_sync_session,
                'access_token': None,
                'thread_id': "test-thread"
            })
        }
        
        args = {} # All fields already in draft
        result = await handle_create_contact(args, mock_services, tenant_id, history=[])
        
        assert result["status"] == "auth_required"
        assert "login card" in result["response_instruction"]
