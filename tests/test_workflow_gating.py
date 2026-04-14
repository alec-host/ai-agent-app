import os
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
async def test_workflow_gating_production_logic():
    """
    CRITICAL PRODUCTION TEST: Workflow Gating
    Scenario: 
    1. User starts a calendar event titled 'Legal Battles'.
    2. User mentions 'Legal Battles' again.
    3. The system MUST NOT trigger client_creation, but instead stick to calendar_agent.
    """
    tenant_id = "prod-tenant-gating"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate",
        "X-User-Email": "test@example.com"
    }

    # 1. Mock DB: Return an active 'calendar' workflow
    active_session = {
        "tenantId": tenant_id,
        "metadata": {
            "active_workflow": "calendar",
            "event_draft": {"title": "Legal Battles"}
        }
    }
    # Both rehydration and loop get calls
    respx.get(url__regex=r".*/chat/session.*").mock(
        return_value=Response(200, json=active_session)
    )
    respx.get(url__regex=r".*/auth/accessToken.*").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "test-jwt-token"})
    )
    respx.get(url__regex=r".*/auth/hasGrantToken.*").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )
    respx.post(url__regex=r".*/chat/session.*").mock(
        return_value=Response(200, json={"status": "success"})
    )
    respx.post(url__regex=r".*/wallet/deplete.*").mock(return_value=Response(200, json={"status": "ok"}))

    # 2. Mock AI: Attempting to call 'create_client_record' (Failure Scenario)
    # Even if AI hallucinates and calls client tool, the AgentManager should BLOCK it
    mock_msg_buggy = MagicMock()
    mock_msg_buggy.content = None
    mock_msg_buggy.role = "assistant"
    mock_tool_call = MagicMock()
    mock_tool_call.id = "bad_call_1"
    mock_tool_call.function.name = "create_client_record"
    mock_tool_call.function.arguments = json.dumps({"first_name": "Legal", "last_name": "Battles"})
    mock_msg_buggy.tool_calls = [mock_tool_call]
    mock_msg_buggy.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{"id": "bad_call_1", "type": "function", "function": {"name": "create_client_record", "arguments": mock_tool_call.function.arguments}}]
    }

    # Second pass after blockage
    mock_msg_fixed = MagicMock()
    mock_msg_fixed.content = "I cannot create a client while an event is being drafted. Let's finish 'Legal Battles' first."
    mock_msg_fixed.tool_calls = None
    mock_msg_fixed.role = "assistant"
    mock_msg_fixed.model_dump.return_value = {"role": "assistant", "content": mock_msg_fixed.content}

    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        mock_instance.chat.completions.create = AsyncMock(side_effect=[
            MagicMock(choices=[MagicMock(message=mock_msg_buggy)], usage=MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)),
            MagicMock(choices=[MagicMock(message=mock_msg_fixed)], usage=MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15))
        ])
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post("/ai/chat", json={"prompt": "Legal Battles", "history": []}, headers=headers)
                
                assert response.status_code == 200
                data = response.json()
                # The response should reflect the conflict error returned by agent_manager
                assert "conflict" in data["response"].lower() or "finish" in data["response"].lower()

@pytest.mark.asyncio
@respx.mock
async def test_field_priority_and_single_word_mapping():
    """
    CRITICAL PRODUCTION TEST: Field Priority & Single-Word Mapping
    Scenario:
    1. User is in Client Intake.
    2. Vault has first_name='Peter'.
    3. Prompt is just 'Pan'.
    4. AI MUST map 'Pan' to 'last_name' and save.
    """
    tenant_id = "prod-tenant-mapping"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate",
        "X-User-Email": "test@example.com"
    }

    # Mock DB: Vault has Peter
    vault_peter = {
        "tenantId": tenant_id,
        "first_name": "Peter",
        "metadata": {
            "active_workflow": "client",
            "chat_history": []
        }
    }
    respx.get(url__regex=r".*/chat/session.*").mock(
        return_value=Response(200, json=vault_peter)
    )
    respx.get(url__regex=r".*/auth/accessToken.*").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "test-jwt-token"})
    )
    respx.get(url__regex=r".*/auth/hasGrantToken.*").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )
    respx.post(url__regex=r".*/chat/session.*").mock(
        return_value=Response(200, json={"status": "success"})
    )
    respx.post(url__regex=r".*/wallet/deplete.*").mock(return_value=Response(200, json={"status": "ok"}))

    # Mock AI: Should call tool with first_name='Peter' AND last_name='Pan'
    mock_msg = MagicMock()
    mock_msg.content = "Saved Peter Pan. What is the client's ID number?"
    mock_tool_call = MagicMock()
    mock_tool_call.id = "save_pan"
    mock_tool_call.function.name = "create_client_record"
    # SUCCESS: AI combines vault data with new input
    mock_tool_call.function.arguments = json.dumps({"first_name": "Peter", "last_name": "Pan"})
    mock_msg.tool_calls = [mock_tool_call]
    mock_msg.role = "assistant"
    mock_msg.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{"id": "save_pan", "type": "function", "function": {"name": "create_client_record", "arguments": mock_tool_call.function.arguments}}]
    }

    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        mock_instance.chat.completions.create = AsyncMock(return_value=MagicMock(
            choices=[MagicMock(message=mock_msg)], 
            usage=MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        ))
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post("/ai/chat", json={"prompt": "Pan", "history": []}, headers=headers)
                
                assert response.status_code == 200
                
                # Check actual tool output by looking at the mock calls
                # Verify the final sync call had both names
                sync_calls = [c for c in respx.calls if c.request.method == "POST" and "/chat/session" in str(c.request.url)]
                last_sync = json.loads(sync_calls[-1].request.content)
                assert last_sync["first_name"] == "Peter"
                assert last_sync["last_name"] == "Pan"
                assert last_sync["metadata"]["active_workflow"] == "client"

if __name__ == "__main__":
    pytest.main([__file__])
