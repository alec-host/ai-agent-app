"""
test_existing_user_handshake.py  (updated for hasGrantToken integration)

Tests the two-step handshake for returning users:
  Step 1: GET /auth/accessToken  -> JWT Provisioner
  Step 2: GET /auth/hasGrantToken -> Calendar Grant Gate (replaced old /events probe)
"""

import os
os.environ["OPENAI_API_KEY"] = "sk-deep-test-key"

import pytest
import respx
import json
from httpx import AsyncClient, ASGITransport, Response
from asgi_lifespan import LifespanManager
from src.main import app
from src.config import settings

BASE = settings.NODE_SERVICE_URL


@pytest.mark.asyncio
@respx.mock
async def test_handshake_existing_user_silent_healing():
    """
    Scenario: Existing user — JWT is recovered from accessToken (silent healing),
    then hasGrantToken confirms the Google Calendar grant is still valid.
    Result: LLM is called and responds.
    """
    tenant_id = "existing-user-123"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    # Step 1: accessToken returns a fresh JWT
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "fresh-token-xyz"})
    )
    # Step 2: hasGrantToken confirms the grant is valid
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True,
                                        "message": "Google Calendar integration is active and valid."})
    )

    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))
    respx.post(f"{BASE}/wallet/deplete").mock(return_value=Response(200, json={"status": "ok"}))

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
                response = await ac.post(
                    "/ai/chat",
                    json={"prompt": "schedule a google meeting", "history": []},
                    headers=headers
                )

                assert response.status_code == 200
                data = response.json()
                # Handshake passed -> LLM response is returned
                assert data.get("response") == "Mocked Assistant Response"

    # Verify BOTH steps were called in the correct order
    access_calls = [c for c in respx.calls if "/auth/accessToken" in str(c.request.url)]
    grant_calls  = [c for c in respx.calls if "/auth/hasGrantToken" in str(c.request.url)]
    assert len(access_calls) >= 1, "Step 1 (accessToken) was not called"
    assert len(grant_calls)  >= 1, "Step 2 (hasGrantToken) was not called"

    # Confirm old live probe is gone: /events?maxResults=1 must NOT appear in the gate
    event_probe_calls = [
        c for c in respx.calls
        if "/events" in str(c.request.url) and "maxResults" in str(c.request.url)
    ]
    assert len(event_probe_calls) == 0, \
        "Old /events?maxResults=1 live probe was still called — should have been replaced by hasGrantToken"


@pytest.mark.asyncio
@respx.mock
async def test_handshake_existing_user_revoked():
    """
    Scenario: Existing user but token revoked.
    Step 1 returns auth_required -> Python app responds with auth button immediately.
    hasGrantToken must NOT be called (no JWT to send it with).
    """
    tenant_id = "revoked-user-456"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    auth_url = "https://google.com/auth"
    # Step 1 confirms no session
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "auth_required", "auth_url": auth_url})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/ai/chat",
                json={"prompt": "schedule a google meeting", "history": []},
                headers=headers
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "auth_required"
            assert data["auth_url"] == auth_url
            assert data["content"] == "Calendar Access Required"

    # hasGrantToken must NOT be called when Step 1 already failed
    grant_calls = [c for c in respx.calls if "/auth/hasGrantToken" in str(c.request.url)]
    assert len(grant_calls) == 0, "check_grant_token was incorrectly called despite Step 1 failing"


@pytest.mark.asyncio
@respx.mock
async def test_handshake_existing_user_refresh_failed():
    """
    Scenario: JWT is obtainable (Step 1 OK) but Google Calendar refresh token
    has expired (hasGrantToken returns exists=True, valid=False).
    Result: auth_required, user must re-consent.
    """
    tenant_id = "refresh-failed-789"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "stale-token"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": True, "valid": False,
            "message": "Google Calendar integration exists but session is invalid. Re-authentication required."
        })
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/ai/chat",
                json={"prompt": "book a google appointment", "history": []},
                headers=headers
            )

            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "auth_required"
            assert data["content"] == "Calendar Access Required"
            assert "auth_url" in data


@pytest.mark.asyncio
@respx.mock
async def test_handshake_streaming_healing():
    """
    Streaming version: Step 1 returns JWT, Step 2 confirms grant ->
    SSE stream begins without an auth_required chunk.
    """
    tenant_id = "stream-heal-789"
    headers = {
        "X-Tenant-ID": tenant_id,
        "X-User-Timezone": "UTC",
        "User-Role": "Associate"
    }

    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "stream-token"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True,
                                        "message": "Google Calendar integration is active and valid."})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json={"tenantId": tenant_id}))

    async with LifespanManager(app) as manager:
        transport = ASGITransport(app=manager.app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            response = await ac.post(
                "/ai/chat/stream",
                json={"prompt": "book me a google appointment", "history": []},
                headers=headers
            )

            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    assert data.get("status") != "auth_required", \
                        "Streaming handshake was blocked when it should have passed the gate."
                    break


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
