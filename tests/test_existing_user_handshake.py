import os
os.environ["OPENAI_API_KEY"] = "sk-deep-test-key"

import pytest
import respx
import json
from httpx import AsyncClient, ASGITransport, Response
from asgi_lifespan import LifespanManager
from src.main import app
from src.config import settings

@pytest.mark.asyncio
@respx.mock
async def test_handshake_existing_user_silent_healing():
    """
    Scenario: Existing user with an expired token.
    1. Python app hits /auth/accessToken (Step 1).
    2. Backend returns {status: ready, jwtToken: 'fresh-token'}.
    3. Python app syncs token and hits /events?maxResults=1 (Step 2).
    4. Step 2 succeeds.
    5. AI proceeds to LLM.
    """
    tenant_id = "existing-user-123"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # Step 1 Mock: Backend returns fresh token
    respx.get(f"{settings.NODE_SERVICE_URL}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "fresh-token-xyz"})
    )

    # Step 2 Mock: Google API Probe (Mocking success)
    respx.get(f"{settings.NODE_SERVICE_URL}/events", params={"maxResults": "1"}).mock(
        return_value=Response(200, json={"status": "success", "items": []})
    )

    # Other necessary mocks
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))
    respx.post(f"{settings.NODE_SERVICE_URL}/wallet/deplete").mock(return_value=Response(200, json={"status": "ok"}))

    from unittest.mock import patch, AsyncMock, MagicMock
    with patch("src.main.AsyncOpenAI") as mock_openai_class:
        mock_instance = mock_openai_class.return_value
        mock_instance.chat.completions.create = AsyncMock(return_value=MagicMock(
            choices=[MagicMock(message=MagicMock(content="Mocked Assistant Response", tool_calls=None))],
            usage=MagicMock(total_tokens=10)
        ))
        
        async with LifespanManager(app) as manager:
            transport = ASGITransport(app=manager.app)
            async with AsyncClient(transport=transport, base_url="http://test") as ac:
                response = await ac.post("/ai/chat", json={"prompt": "schedule a meeting", "history": []}, headers=headers)
                
                assert response.status_code == 200
                data = response.json()
                # If handshake passed, we should get the content from the mocked LLM
                assert data.get("response") == "Mocked Assistant Response"

@pytest.mark.asyncio
@respx.mock
async def test_handshake_existing_user_revoked():
    """
    Scenario: Existing user but token revoked (Redis has nothing or Google rejects refresh).
    1. Python app hits /auth/accessToken (Step 1).
    2. Backend returns {status: auth_required, auth_url: '...'}.
    3. Python app MUST respond immediately with auth button.
    """
    tenant_id = "revoked-user-456"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # Step 1 Mock: Backend confirms auth is dead
    auth_url = "https://google.com/auth"
    respx.get(f"{settings.NODE_SERVICE_URL}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "auth_required", "auth_url": auth_url})
    )

    # Mock rehydration
    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/ai/chat", json={"prompt": "create event", "history": []}, headers=headers)
            
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "auth_required"
            assert data["auth_url"] == auth_url
            assert data["content"] == "Calendar Access Required"

@pytest.mark.asyncio
@respx.mock
async def test_handshake_streaming_healing():
    """
    Streaming version of silent healing.
    """
    tenant_id = "stream-heal-789"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # Mock Step 1
    respx.get(f"{settings.NODE_SERVICE_URL}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "stream-token"})
    )
    # Mock Step 2 (Success)
    respx.get(f"{settings.NODE_SERVICE_URL}/events", params={"maxResults": "1"}).mock(
        return_value=Response(200, json={"status": "success", "items": []})
    )

    respx.get(f"{settings.NODE_SERVICE_URL}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post("/ai/chat/stream", json={"prompt": "book me", "history": []}, headers=headers)
            
            # Check the first chunk. If it's NOT an auth chunk, it passed.
            # In a success scenario, the next thing it does is call OpenAI.
            
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    assert data.get("status") != "auth_required", "Streaming handshake blocked when it should have healed."
                    break

if __name__ == "__main__":
    pytest.main([__file__])
