import os
os.environ["OPENAI_API_KEY"] = "sk-test-key"

import pytest
import respx
import json
from httpx import AsyncClient, ASGITransport, Response
from unittest.mock import MagicMock, patch, AsyncMock
from src.main import app
from src.config import settings

@pytest.mark.asyncio
@respx.mock
async def test_advanced_event_creation_flow():
    """
    PRODUCTION TEST: Advanced Event Creation
    Validates:
    1. Gating for Title/Time.
    2. Gating for Optional Fields (Summary, Attendees, Location).
    3. Final Save with all fields.
    4. Aggressive Session Wipe (setting fields to None).
    """
    tenant_id = "prod-test-calendar"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # --- 1. MOCK BACKEND RESPONSES ---
    
    # Mock Token Ready
    respx.get(f"{settings.NODE_SERVICE_URL}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready"})
    )
    
    # Mock Wallet
    respx.post(f"{settings.NODE_SERVICE_URL}/wallet/deplete").mock(
        return_value=Response(200, json={"status": "ok"})
    )

    # Mock Session Sync (POST)
    sync_mock = respx.post(f"{settings.NODE_SERVICE_URL}/chat/session").mock(
        return_value=Response(200, json={"status": "success"})
    )

    # Mock Event Execution (POST to /events)
    event_mock = respx.post(f"{settings.NODE_SERVICE_URL}/events").mock(
        return_value=Response(200, json={"status": "success", "id": "evt_123"})
    )

    # Mock Session Clear (DELETE)
    clear_mock = respx.delete(f"{settings.NODE_SERVICE_URL}/chat/session").mock(
        return_value=Response(200, json={"status": "success"})
    )

    # --- 2. STEP 1: GATING FOR OPTIONAL FIELDS ---
    # Scenario: Already have Title and Time, but HAVEN'T asked for optional fields yet.
    
    active_session_step1 = {
        "tenantId": tenant_id,
        "metadata": {
            "active_workflow": "calendar",
            "event_draft": {
                "title": "Strategy Meeting",
                "startTime": "2026-03-20T10:00:00Z",
                # "optional_fields_requested" is MISSING
            }
        }
    }
    
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session").mock(
        return_value=Response(200, json=active_session_step1)
    )

    transport = ASGITransport(app=app)
    from asgi_lifespan import LifespanManager
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # User sends something, AI tries to schedule
            payload = {
                "prompt": "Book it please",
                "history": []
            }
            
            # We need to mock the AI call to trigger schedule_event
            mock_ai_resp = MagicMock()
            mock_ai_resp.choices = [MagicMock()]
            message_mock = MagicMock()
            message_mock.content = "Sure, booking now..."
            message_mock.role = "assistant"
            
            # Proper tool call structure for OpenAI SDK mock
            tool_call = MagicMock()
            tool_call.id = "call_1"
            tool_call.type = "function"
            tool_call.function.name = "schedule_event"
            tool_call.function.arguments = json.dumps({
                "title": "Strategy Meeting",
                "startTime": "2026-03-20T10:00:00Z"
            })
            message_mock.tool_calls = [tool_call]
            message_mock.model_dump.return_value = {
                "role": "assistant",
                "content": message_mock.content,
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "schedule_event", "arguments": tool_call.function.arguments}
                }]
            }
            mock_ai_resp.choices[0].message = message_mock

            with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_ai_resp
                response = await ac.post("/ai/chat", json=payload, headers=headers)
                
                # Verify the response history contains the gating instruction
                data = response.json()
                if "history" not in data:
                    print(f"DEBUG FAIL STEP 1: Status {response.status_code} | Body: {data}")
                
                # The tool result is the last message in history
                tool_output_node = data["history"][-1]
                tool_output = json.loads(tool_output_node["content"])
                
                assert tool_output["status"] == "partial_success"
                assert "explicitly ask the user" in tool_output["response_instruction"]
                
                # Verify that the sync payload included the flag
                last_sync_payload = json.loads(sync_mock.calls[-1].request.content)
                assert last_sync_payload["metadata"]["event_draft"]["optional_fields_requested"] is True

        # --- 3. STEP 2: FINAL SAVE & WIPE ---
        # Scenario: Title, Time, and now we provide Optional Fields.
        
        active_session_step2 = {
            "tenantId": tenant_id,
            "metadata": {
                "active_workflow": "calendar",
                "event_draft": {
                    "title": "Strategy Meeting",
                    "startTime": "2026-03-20T10:00:00Z",
                    "optional_fields_requested": True
                }
            }
        }
        
        respx.get(f"{settings.NODE_SERVICE_URL}/chat/session").mock(
            return_value=Response(200, json=active_session_step2)
        )

        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {
                "prompt": "Summary is q2 planning, location is room 5, attendees are bob@test.com",
                "history": []
            }
            
            mock_ai_resp_final = MagicMock()
            mock_ai_resp_final.choices = [MagicMock()]
            message_mock_final = MagicMock()
            message_mock_final.content = "Understood. Scheduling..."
            message_mock_final.role = "assistant"
            
            tool_call_final = MagicMock()
            tool_call_final.id = "call_final"
            tool_call_final.type = "function"
            tool_call_final.function.name = "schedule_event"
            tool_call_final.function.arguments = json.dumps({
                "title": "Strategy Meeting",
                "startTime": "2026-03-20T10:00:00Z",
                "description": "q2 planning",
                "location": "room 5",
                "attendees": ["bob@test.com"]
            })
            message_mock_final.tool_calls = [tool_call_final]
            message_mock_final.model_dump.return_value = {
                "role": "assistant",
                "content": message_mock_final.content,
                "tool_calls": [{
                    "id": "call_final",
                    "type": "function",
                    "function": {"name": "schedule_event", "arguments": tool_call_final.function.arguments}
                }]
            }
            mock_ai_resp_final.choices[0].message = message_mock_final

            with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create_final:
                mock_create_final.return_value = mock_ai_resp_final
                response = await ac.post("/ai/chat", json=payload, headers=headers)
                
                # Verify tool was called with all parameters (in handle_agent_query loop)
                evt_call = event_mock.calls[-1].request
                evt_payload = json.loads(evt_call.content)
                assert evt_payload["title"] == "Strategy Meeting"
                assert evt_payload["summary"] == "q2 planning" 
                assert evt_payload["location"] == "room 5"
                assert "bob@test.com" in evt_payload["attendees"]

                # CRITICAL: Verify the WIPE payload (Explicit None values)
                last_sync = json.loads(sync_mock.calls[-1].request.content)
                wipe_draft = last_sync["metadata"]["event_draft"]
                assert wipe_draft["title"] is None
                assert wipe_draft["startTime"] is None
                assert wipe_draft["summary"] is None
                assert wipe_draft["optional_fields_requested"] is False
                
                # Verify delete was called
                assert clear_mock.called

    print("\n✅ PRODUCTION EVENT CREATION TEST PASSED!")
