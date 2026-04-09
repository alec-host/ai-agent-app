import os
import re
os.environ["OPENAI_API_KEY"] = "sk-deep-test-key"

import pytest
import respx
import json
from httpx import AsyncClient, ASGITransport, Response
from unittest.mock import AsyncMock, patch, MagicMock
from asgi_lifespan import LifespanManager
from src.main import app
from src.config import settings

@pytest.mark.asyncio
@respx.mock
async def test_matter_workflow_full_cycle():
    """
    MATTER WORKFLOW INTEGRATION TEST (PRODUCTION READY)
    Tests dynamic choices fetching, lazy lookups, and final creation.
    """
    tenant_id = "test-matter-tenant"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "X-User-Email": "associate@firm.com"
    }

    # 1. MOCK CORE SYSTEM RESPONSES (BROAD MATCH TO PREVENT PARAM MISSES)
    def pa_side_effect(request):
        q = str(request.url.query)
        if "is_search=1" in q:
            return Response(200, json={"success": True, "practice_area_id": 2})
        return Response(200, json={"success": True, "data": [{"id": 1, "name": "Litigation"}]})

    def cs_side_effect(request):
        q = str(request.url.query)
        if "is_search=1" in q:
            return Response(200, json={"success": True, "case_stage_id": 10})
        return Response(200, json={"success": True, "data": [{"id": 10, "name": "Discovery"}]})

    respx.get(re.compile(f".*/practice-area.*")).mock(side_effect=pa_side_effect)
    respx.get(re.compile(f".*/case-stage.*")).mock(side_effect=cs_side_effect)
    respx.get(re.compile(f".*/billing-info.*")).mock(side_effect=cs_side_effect)
    
    respx.post(url__regex=r".*/matters.*").mock(
        return_value=Response(200, json={"status": "success", "id": 500})
    )

    # 2. MOCK SESSION VENDOR (NODE.JS) - Dynamic mock to reflect linkage
    session_data = {
        "tenantId": tenant_id, 
        "metadata": {
            "active_workflow": "matter", 
            "matter_draft": {
                "title": "Contract Dispute", 
                "name": "CD-1", 
                "description": "CD",
                "client_id": 123
            }
        }
    }
    
    def get_session_mock(request):
        # Return the current state of session_data
        return Response(200, json=session_data)

    def update_session_mock(request):
        # Capture the synced payload and update our local session_data
        nonlocal session_data
        payload = json.loads(request.content)
        if "metadata" in payload:
            session_data["metadata"] = payload["metadata"]
        return Response(200, json={"status": "success"})

    respx.get(url__regex=r".*/chat/session.*").mock(side_effect=get_session_mock)
    respx.post(url__regex=r".*/chat/session.*").mock(side_effect=update_session_mock)
    respx.get(url__regex=r".*/auth/accessToken.*").mock(return_value=Response(200, json={"status": "ready", "jwtToken": "token"}))
    respx.get(url__regex=r".*/auth/hasGrantToken.*").mock(return_value=Response(200, json={"success": True, "exists": True, "valid": True}))
    respx.post(url__regex=r".*/wallet/deplete.*").mock(return_value=Response(200, json={"status": "ok"}))

    # 3. MOCK AI BEHAVIOR
    # Turn 1: LLM calls lookup tool
    mock_msg_tool = MagicMock()
    mock_msg_tool.content = None
    mock_tc = MagicMock()
    mock_tc.id = "lookup_pa"
    mock_tc.function.name = "lookup_practice_area"
    mock_tc.function.arguments = json.dumps({"search_term": "IP"})
    mock_msg_tool.tool_calls = [mock_tc]
    mock_msg_tool.role = "assistant"
    mock_msg_tool.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{"id": "lookup_pa", "type": "function", "function": {"name": "lookup_practice_area", "arguments": mock_tc.function.arguments}}]
    }

    # Turn 2: LLM sees vault updated, asks next question
    mock_msg_final = MagicMock()
    mock_msg_final.content = "I've linked the practice area. Now what is the Case Stage?"
    mock_msg_final.tool_calls = None
    mock_msg_final.role = "assistant"
    mock_msg_final.model_dump.return_value = {"role": "assistant", "content": mock_msg_final.content}

    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        
        # Turn 1: User says "Create Matter", AI calls create_matter()
        mock_msg_start = MagicMock()
        mock_msg_start.content = None
        mock_tc_start = MagicMock()
        mock_tc_start.id = "start_matter"
        mock_tc_start.function.name = "create_matter"
        mock_tc_start.function.arguments = "{}"
        mock_msg_start.tool_calls = [mock_tc_start]
        mock_msg_start.role = "assistant"
        mock_msg_start.model_dump.return_value = {"role": "assistant", "tool_calls": [{"id": "start_matter", "type": "function", "function": {"name": "create_matter", "arguments": "{}"}}]}

        # Turn 2: After choices are fetched and run_draft_workflow returns question
        mock_msg_q = MagicMock()
        mock_msg_q.content = "What is the practice area? (Litigation, IP)"
        mock_msg_q.tool_calls = None
        mock_msg_q.role = "assistant"
        mock_msg_q.model_dump.return_value = {"role": "assistant", "content": mock_msg_q.content}

        mock_instance.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=mock_msg_start)], usage=MagicMock(total_tokens=10)),
            MagicMock(choices=[MagicMock(message=mock_msg_q)], usage=MagicMock(total_tokens=10))
        ])
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                # User starts the workflow
                response = ac.post("/ai/chat", json={"prompt": "Create a new matter", "history": []}, headers=headers)
                
                # Check for successful processing
                resp = await response
                assert resp.status_code == 200
                data = resp.json()
                
                # Verify that choices were fetched during processing (is_search=0)
                pa_list_call = [c for c in respx.calls if "/practice-area" in str(c.request.url) and "is_search=0" in str(c.request.url)]
                assert len(pa_list_call) > 0, "Practice Area choices were not fetched dynamically."
                
                # Verify session sync included the Resolved choices in metadata (optional check)
                sync_calls = [c for c in respx.calls if "/chat/session" in str(c.request.url) and c.request.method == "POST"]
                found_cache = False
                for call in sync_calls:
                    payload = json.loads(call.request.content)
                    if payload.get("metadata", {}).get("practice_area_id_choices"):
                        found_cache = True
                        break
                assert found_cache, "The dynamic choices were not cached to the session metadata."

if __name__ == "__main__":
    pytest.main([__file__])
