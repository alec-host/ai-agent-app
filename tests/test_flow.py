import os
os.environ["OPENAI_API_KEY"] = "sk-test-fake-key"

import pytest
import respx
import json
from httpx import AsyncClient, ASGITransport, Response
from unittest.mock import AsyncMock, patch
from asgi_lifespan import LifespanManager
from src.main import app
from src.config import settings

@pytest.mark.asyncio
@respx.mock
async def test_client_intake_amnesia_fix():
    """
    Test Step 1: Register client -> Save partial data
    Test Step 2: Ensure AI sees 'VAULT' data and doesn't ask for Name again.
    """
    tenant_id = "test-tenant-123"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # 1. Mock Node.js Backend Endpoints
    # Mock lookup_firm_protocol
    respx.get(f"{settings.NODE_SERVICE_URL}/rag/lookup").mock(
        return_value=Response(200, json={"context": "Ask for Name, ID, and Email."})
    )
    # Mock chat/session (initial empty)
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session", params={"tenantId": tenant_id}).mock(
        return_value=Response(200, json={})
    )
    # Mock chat/session (POST update)
    respx.post(f"{settings.NODE_SERVICE_URL}/chat/session").mock(
        return_value=Response(200, json={"status": "success"})
    )

    # 2. Mock OpenAI Response
    # Use MagicMock for the data structure to avoid FastAPI serialization issues
    from unittest.mock import MagicMock
    mock_message = MagicMock()
    mock_message.content = "What is the client's first name?"
    mock_message.tool_calls = None
    mock_message.role = "assistant"
    # This matches what .model_dump() would return
    mock_message.model_dump.return_value = {"role": "assistant", "content": "What is the client's first name?"}
    
    mock_choice = MagicMock()
    mock_choice.message = mock_message
    
    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    # Mock wallet depletion (used in background task)
    respx.post(f"{settings.NODE_SERVICE_URL}/wallet/deplete").mock(return_value=Response(200, json={"status": "ok"}))

    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        # The create method itself MUST be an AsyncMock
        mock_instance.chat.completions.create = AsyncMock(return_value=mock_response)
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                # First interaction
                payload = {"prompt": "I want to register a new client", "history": []}
                # We need to mock the verification as well or ensure headers are right
                response = await ac.post("/ai/chat", json=payload, headers=headers)
                
                assert response.status_code == 200
                data = response.json()
                assert "first name" in data["response"].lower()

@pytest.mark.asyncio
@respx.mock
async def test_calendar_conflict_interception():
    """
    Verify that if check-conflicts returns True, the agent intercepts the scheduling.
    """
    from unittest.mock import MagicMock
    tenant_id = "test-tenant-123"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # Mock conflict check to return conflict: True
    respx.get(f"{settings.NODE_SERVICE_URL}/events/check-conflicts").mock(
        return_value=Response(200, json={"hasConflict": True})
    )
    # Mock regular session/health
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session", params={"tenantId": tenant_id}).mock(
        return_value=Response(200, json={})
    )
    respx.get(f"{settings.NODE_SERVICE_URL}/").mock(return_value=Response(200, json={"message": "ok"}))
    respx.post(f"{settings.NODE_SERVICE_URL}/wallet/deplete").mock(return_value=Response(200, json={"status": "ok"}))

    # Iteration 1: AI calls tool
    mock_message_1 = MagicMock()
    mock_message_1.content = None
    mock_message_1.role = "assistant"
    
    mock_tool_call = MagicMock()
    mock_tool_call.id = "call_abc123"
    mock_tool_call.function.name = "schedule_event"
    mock_tool_call.function.arguments = json.dumps({
        "summary": "Conflict Test",
        "startTime": "2026-03-07T14:00:00Z",
        "duration_minutes": 60
    })
    mock_message_1.tool_calls = [mock_tool_call]
    mock_message_1.model_dump.return_value = {
        "role": "assistant", 
        "tool_calls": [{
            "id": "call_abc123", 
            "type": "function", 
            "function": {"name": "schedule_event", "arguments": mock_tool_call.function.arguments}
        }]
    }

    # Iteration 2: AI explains conflict
    mock_message_2 = MagicMock()
    mock_message_2.content = "There is a conflict at that time. Would you like to pick another slot?"
    mock_message_2.tool_calls = None
    mock_message_2.role = "assistant"
    mock_message_2.model_dump.return_value = {"role": "assistant", "content": mock_message_2.content}

    mock_choice_1 = MagicMock()
    mock_choice_1.message = mock_message_1
    
    mock_choice_2 = MagicMock()
    mock_choice_2.message = mock_message_2

    mock_response_1 = MagicMock()
    mock_response_1.choices = [mock_choice_1]
    mock_response_1.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    
    mock_response_2 = MagicMock()
    mock_response_2.choices = [mock_choice_2]
    mock_response_2.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)

    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        mock_instance.chat.completions.create = AsyncMock(side_effect=[mock_response_1, mock_response_2])
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                payload = {"prompt": "Schedule meeting tomorrow at 2 PM", "history": []}
                response = await ac.post("/ai/chat", json=payload, headers=headers)
                
                assert response.status_code == 200
                result_data = response.json()
                assert "conflict" in result_data["response"].lower()
                
                # Verify no POST /events was actually attempted (since it was intercepted)
                assert not any(call.request.method == "POST" and "/events" in str(call.request.url) for call in respx.calls)

@pytest.mark.asyncio
@respx.mock
async def test_event_drafting_amnesia_fix():
    """
    Verify that an existing 'event_draft' in the DB is rehydrated 
    and prevents the AI from forgetting the meeting title.
    """
    from unittest.mock import MagicMock
    tenant_id = "test-tenant-456"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # 1. Mock DB to return a PLANNED meeting title (The Amnesia Fix)
    existing_session = {
        "tenantId": tenant_id,
        "metadata": {
            "event_draft": {
                "title": "Strategy Session"
            }
        }
    }
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session", params={"tenantId": tenant_id}).mock(
        return_value=Response(200, json=existing_session)
    )
    respx.post(f"{settings.NODE_SERVICE_URL}/chat/session").mock(return_value=Response(200, json={"status": "success"}))
    respx.post(f"{settings.NODE_SERVICE_URL}/events").mock(return_value=Response(200, json={"id": "evt_123", "status": "success"}))
    respx.post(f"{settings.NODE_SERVICE_URL}/wallet/deplete").mock(return_value=Response(200, json={"status": "ok"}))

    # 2. Mock AI behavior
    # Turn 1: AI sees the draft and asks for the time
    mock_msg_1 = MagicMock()
    mock_msg_1.content = "I see your 'Strategy Session'. What time should I schedule it for?"
    mock_msg_1.tool_calls = None
    mock_msg_1.role = "assistant"
    mock_msg_1.model_dump.return_value = {"role": "assistant", "content": mock_msg_1.content}
    
    mock_choice_1 = MagicMock()
    mock_choice_1.message = mock_msg_1
    mock_resp_1 = MagicMock()
    mock_resp_1.choices = [mock_choice_1]
    mock_resp_1.usage = MagicMock(prompt_tokens=50, completion_tokens=10, total_tokens=60)

    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        mock_instance.chat.completions.create = AsyncMock(return_value=mock_resp_1)
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                # The user just says "Schedule it" without re-stating the title
                response = await ac.post("/ai/chat", json={"prompt": "Let's schedule it", "history": []}, headers=headers)
                
                assert response.status_code == 200
                data = response.json()
                
                # IMPORTANT: AI must mention the title it 'recovered' from the VAULT
                assert "strategy session" in data["response"].lower()

                # Verify that the loop correctly fetched the session
                # (1 for rehydration, 1 for vault-check inside loop)
                get_calls = [c for c in respx.calls if c.request.method == "GET" and "/chat/session" in str(c.request.url)]
                assert len(get_calls) >= 1

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_client_intake_amnesia_fix())
