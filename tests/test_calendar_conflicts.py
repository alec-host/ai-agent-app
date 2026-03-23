import pytest
import respx
import json
from httpx import Response, AsyncClient
from src.main import app
from src.remote_services.google_core import GoogleCalendarClient
from src.agents.calendar_agent import handle_calendar
from src.config import settings

BASE = settings.NODE_SERVICE_URL

@pytest.mark.asyncio
@respx.mock
async def test_schedule_event_blocks_on_conflict():
    tenant_id = "test_tenant_conflict"
    
    # 1. Mock session fetch (active calendar workflow)
    respx.get(url__regex=rf".*/chat/session\?tenantId={tenant_id}.*").mock(
        return_value=Response(200, json={
            "metadata": {
                "active_workflow": "calendar",
                "event_draft": {
                    "title": "Conflicting Meeting",
                    "startTime": "2026-03-12T19:00:00",
                    "summary_requested": True,
                    "attendees_requested": True,
                    "location_requested": True
                }
            }
        })
    )
    
    # 2. Mock hasGrantToken
    respx.get(url__regex=rf".*/auth/hasGrantToken\?tenant_id={tenant_id}.*").mock(
        return_value=Response(200, json={"success": True, "valid": True})
    )

    # 3. Mock conflict check - RETURN CONFLICT
    import re
    respx.get(url__regex=r".*/events/check-conflicts.*").mock(
        return_value=Response(200, json={"success": True, "hasConflict": True})
    )

    async with AsyncClient(base_url=BASE) as client:
        service = GoogleCalendarClient(tenant_id, client, correlation_id="mock-id")
        service.set_auth_token("mock_jwt")
        
        args = {
            "title": "Conflicting Meeting",
            "startTime": "2026-03-12T19:00:00",
            "duration_minutes": 60
        }
        
        result = await handle_calendar("schedule_event", args, service, "Associate", history=[])
        
        # Verify it blocked the booking
        assert result["status"] == "partial_success"
        assert "Conflict detected" in result["message"]
        assert "requested time slot is already booked" in result["response_instruction"]
        
        # Verify NO POST to /events was made
        assert not any(call.request.method == "POST" and "/events" in str(call.request.url) for call in respx.calls)

@pytest.mark.asyncio
@respx.mock
async def test_schedule_event_proceeds_if_no_conflict():
    tenant_id = "test_tenant_no_conflict"
    
    # 1. Mock session fetch
    respx.get(url__regex=rf".*/chat/session\?tenantId={tenant_id}.*").mock(
        return_value=Response(200, json={
            "metadata": {
                "active_workflow": "calendar",
                "event_draft": {
                    "title": "Free Slot Meeting",
                    "startTime": "2026-03-12T10:00:00",
                    "summary_requested": True,
                    "attendees_requested": True,
                    "location_requested": True
                }
            }
        })
    )
    
    # 2. Mock hasGrantToken
    respx.get(url__regex=rf".*/auth/hasGrantToken\?tenant_id={tenant_id}.*").mock(
        return_value=Response(200, json={"success": True, "valid": True})
    )

    # 3. Mock conflict check - NO CONFLICT
    respx.get(url__regex=r".*/events/check-conflicts.*").mock(
        return_value=Response(200, json={"success": True, "hasConflict": False})
    )

    # 4. Mock session sync
    respx.post(f"{BASE}/chat/session").mock(return_value=Response(200, json={"success": True}))

    # 5. Mock event creation
    respx.post(f"{BASE}/events").mock(
        return_value=Response(200, json={"status": "success", "id": "new_event_123"})
    )
    
    # 6. Mock session clear
    respx.delete(url__regex=r".*/chat/session.*").mock(return_value=Response(200, json={"success": True}))

    async with AsyncClient(base_url=BASE) as client:
        service = GoogleCalendarClient(tenant_id, client, correlation_id="mock-id")
        service.set_auth_token("mock_jwt")
        
        args = {
            "title": "Free Slot Meeting",
            "startTime": "2026-03-12T10:00:00",
            "duration_minutes": 60
        }
        
        result = await handle_calendar("schedule_event", args, service, "Associate", history=[])
        
        # Verify success
        assert result["status"] == "success"
        
        # Verify POST to /events WAS made
        assert any(call.request.method == "POST" and "/events" in str(call.request.url) for call in respx.calls)
