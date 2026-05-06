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
    
    async def mock_get_session(t, user_email=None):
        return {"metadata": {"active_workflow": "client", "client_draft": {"client_email": "found@example.com"}}}
    async def mock_sync_session(p):
        sync_recorded.append(p)
        return True

    from unittest.mock import AsyncMock
    mock_calendar = AsyncMock()
    mock_calendar.get_client_session = mock_get_session
    mock_calendar.sync_client_session = mock_sync_session
    mock_calendar.thread_id = "t1"
    mock_calendar.access_token = "s1"
    mock_services = {'calendar': mock_calendar, 'session': mock_calendar}

    with respx.mock:
        respx.get(url__regex=r".*/search-contact.*").mock(return_value=Response(
            200, json={"status": "success", "contact_id": "real-cont-123"}
        ))
        
        # --- HARDENED: Route through the actual Dispatcher ---
        from src.agent_manager import execute_tool_call
        class MockTool:
            def __init__(self, name, args):
                self.function = type('obj', (object,), {"name": name, "arguments": json.dumps(args)})

        mock_tool = MockTool("create_client_record", {"client_email": "found@example.com"})
        
        await execute_tool_call(mock_tool, mock_services, "user", tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email="test@example.com")
        
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
    
    async def mock_get_session(t, user_email=None):
        return {"metadata": {"active_workflow": "client", "client_draft": {}}}
    async def mock_sync_session(p):
        sync_recorded.append(p)
        return True

    from unittest.mock import AsyncMock
    mock_calendar = AsyncMock()
    mock_calendar.get_client_session = mock_get_session
    mock_calendar.sync_client_session = mock_sync_session
    mock_calendar.thread_id = "t2"
    mock_calendar.access_token = "s2"
    mock_services = {'calendar': mock_calendar, 'session': mock_calendar}

    with respx.mock:
        # Mock payload: { "success": true, "country_id": 15, "message": "Retreived country id successfully" }
        respx.get(url__regex=r".*/countries.*").mock(return_value=Response(
            200, json={"success": True, "country_id": 15, "message": "Retreived country id successfully"}
        ))
        
        # --- HARDENED: Route through the actual Dispatcher ---
        from src.agent_manager import execute_tool_call
        class MockTool:
            def __init__(self, name, args):
                self.function = type('obj', (object,), {"name": name, "arguments": json.dumps(args)})

        mock_tool = MockTool("lookup_countries", {"search": "Kenya"})
        
        result = await execute_tool_call(mock_tool, mock_services, "user", tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email="test@example.com")
        
        assert result["status"] == "success"
        assert result["country_id"] == 15
        
        # Verify sync had country_id
        assert len(sync_recorded) > 0
        metadata = sync_recorded[-1]["metadata"]
        assert metadata["client_draft"]["country_id"] == 15

if __name__ == "__main__":
    pytest.main([__file__])
