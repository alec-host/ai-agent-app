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
async def test_simple_core_event():
    tenant_id = "simple-test"
    headers = {"X-Tenant-ID": tenant_id, "X-User-Email": "user@test.com"}
    
    # 1. Mock External Services
    respx.get(f"{settings.NODE_SERVICE_URL}/auth/accessToken").mock(return_value=Response(200, json={"status": "ready"}))
    respx.get(f"{settings.NODE_REMOTE_SERVICE_URL}/auth/check-session").mock(return_value=Response(200, json={"status": "ready"}))
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id, "metadata": {}}))
    respx.post(f"{settings.NODE_SERVICE_URL}/chat/session").mock(return_value=Response(200, json={"status": "success"}))
    respx.post(f"{settings.NODE_SERVICE_URL}/wallet/deplete").mock(return_value=Response(200, json={"status": "ok"}))

    transport = ASGITransport(app=app)
    from asgi_lifespan import LifespanManager
    async with LifespanManager(app):
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            
            # --- TURN 1: PROVIDE TITLE ---
            payload = {"prompt": "Schedule a strategy meeting", "history": []}
            
            # Mock tool call structure as pure dict then wrap in MagicMock for OpenAI SDK behavior
            tool_call_dict = {
                "id": "call_1",
                "type": "function",
                "function": {"name": "create_standard_event", "arguments": json.dumps({"title": "Strategy Meeting"})}
            }
            
            # AI Assistant Message dict
            assistant_dict = {
                "role": "assistant",
                "content": "Let's start drafting...",
                "tool_calls": [tool_call_dict]
            }
            
            # Simulated OpenAI response object
            mock_ai_resp = MagicMock()
            message_mock = MagicMock()
            message_mock.role = "assistant"
            message_mock.content = "Let's start drafting..."
            message_mock.tool_calls = [MagicMock()]
            message_mock.tool_calls[0].id = "call_1"
            message_mock.tool_calls[0].type = "function"
            message_mock.tool_calls[0].function.name = "create_standard_event"
            message_mock.tool_calls[0].function.arguments = json.dumps({"title": "Strategy Meeting"})
            
            # IMPORTANT: model_dump MUST RETURN PURE DICT
            message_mock.model_dump.return_value = assistant_dict
            
            mock_ai_resp.choices = [MagicMock(message=message_mock)]
            mock_ai_resp.usage = MagicMock(total_tokens=100)

            with patch("openai.resources.chat.completions.AsyncCompletions.create", new_callable=AsyncMock) as mock_create:
                mock_create.return_value = mock_ai_resp
                response = await ac.post("/ai/chat", json=payload, headers=headers)
                
                assert response.status_code == 200
                data = response.json()
                assert "Strategy Meeting" in str(data)
