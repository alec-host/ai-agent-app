import pytest
import json
import respx
from httpx import Response
from src.agents.core_agent import handle_create_client, handle_lookup_countries
from src.config import settings

@pytest.mark.asyncio
async def test_auto_lookup_and_linking():
    """
    Test that contact_id is automatically linked if search finds a match.
    """
    tenant_id = "test-auto-link"
    sync_recorded = []
    
    async def mock_get_session(t):
        return {"metadata": {"active_workflow": "client", "client_draft": {"client_email": "found@example.com"}}}
    async def mock_sync_session(p):
        sync_recorded.append(p)
        return True

    mock_services = {
        'calendar': type('obj', (object,), {
            'get_client_session': mock_get_session, 
            'sync_client_session': mock_sync_session,
            'thread_id': "t1",
            'access_token': "s1"
        })
    }

    with respx.mock:
        respx.get(f"{settings.NODE_REMOTE_SERVICE_URL}/search-contact").mock(return_value=Response(
            200, json={"status": "success", "contact_id": "real-cont-123"}
        ))
        
        # client_email is provided
        await handle_create_client({"client_email": "found@example.com"}, mock_services, tenant_id, history=[])
        
        # Verify sync had contact_id
        assert len(sync_recorded) > 0
        metadata = sync_recorded[-1]["metadata"]
        assert metadata["client_draft"]["contact_id"] == "real-cont-123"

@pytest.mark.asyncio
async def test_country_direct_id_payload():
    """
    Test handle_lookup_countries with the newly defined Node.js payload.
    """
    tenant_id = "test-country-pay"
    sync_recorded = []
    
    async def mock_get_session(t):
        return {"metadata": {"active_workflow": "client", "client_draft": {}}}
    async def mock_sync_session(p):
        sync_recorded.append(p)
        return True

    mock_services = {
        'calendar': type('obj', (object,), {
            'get_client_session': mock_get_session, 
            'sync_client_session': mock_sync_session,
            'thread_id': "t2",
            'access_token': "s2"
        })
    }

    with respx.mock:
        # Mock payload: { "success": true, "country_id": 15, "message": "Retreived country id successfully" }
        respx.get(f"{settings.NODE_REMOTE_SERVICE_URL}/countries").mock(return_value=Response(
            200, json={"success": True, "country_id": 15, "message": "Retreived country id successfully"}
        ))
        
        result = await handle_lookup_countries({"search": "Kenya"}, mock_services, tenant_id)
        
        assert result["status"] == "success"
        assert result["country_id"] == 15
        
        # Verify sync had country_id
        assert len(sync_recorded) > 0
        metadata = sync_recorded[-1]["metadata"]
        assert metadata["client_draft"]["country_id"] == 15

if __name__ == "__main__":
    pytest.main([__file__])
