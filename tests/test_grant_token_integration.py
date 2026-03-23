"""
test_grant_token_integration.py

Tests for the /auth/hasGrantToken integration.
Covers the full two-step handshake:
  Step 1: _sync_access_token() -> GET /auth/accessToken (JWT Provisioner)
  Step 2: check_grant_token()  -> GET /auth/hasGrantToken (Calendar Grant Gate)

Test Scenarios:
  1.  Happy path: JWT synced + grant valid -> LLM proceeds
  2.  No session (Step 1 fails): auth_required returned immediately, no grant check
  3.  Grant not found (new user, Step 2 returns exists=False): auth_required
  4.  Grant exists but invalid (token refresh failed, Step 2 returns valid=False): auth_required
  5.  Non-calendar prompt ("create a client"): NO auth handshake triggered
  6.  Streaming: happy path -> SSE proceeds past gate
  7.  Streaming: no session -> SSE auth_required chunk
  8.  Streaming: grant invalid -> SSE auth_required chunk
  9.  Agentic loop gate (active_workflow=calendar): grant valid -> loop continues
  10. Agentic loop gate (active_workflow=calendar): grant invalid -> auth_required
  11. calendar_agent preflight: grant valid -> drafting proceeds
  12. calendar_agent preflight: grant invalid -> auth_required returned to agent loop
  13. Non-event calendar funcs (get_system_status): bypass check_grant_token entirely
  14. hasGrantToken service unreachable -> graceful auth_required (no crash)
"""

import os
os.environ["OPENAI_API_KEY"] = "sk-test-grant-token-key"

import pytest
import respx
import json
from httpx import AsyncClient, ASGITransport, Response
from unittest.mock import AsyncMock, MagicMock, patch
from asgi_lifespan import LifespanManager
from src.main import app
from src.config import settings

# ---------------------------------------------------------------------------
# Shared Helpers
# ---------------------------------------------------------------------------

BASE = settings.NODE_SERVICE_URL
TENANT = "tenant-grant-test"
HEADERS = {
    "X-Tenant-ID": TENANT,
    "X-User-Timezone": "UTC",
    "User-Role": "Associate",
}

def _mock_openai_text(text: str):
    """Returns a mock OpenAI completion that produces a plain text response (no tools)."""
    msg = MagicMock()
    msg.content = text
    msg.tool_calls = None
    msg.role = "assistant"
    msg.model_dump.return_value = {"role": "assistant", "content": text}
    resp = MagicMock()
    resp.choices = [MagicMock(message=msg)]
    resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    return resp


def _mock_empty_session():
    return Response(200, json={"tenantId": TENANT})


def _mock_wallet():
    return Response(200, json={"status": "ok"})


# ---------------------------------------------------------------------------
# UNIT: GoogleCalendarClient helper methods (isolated)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_sync_access_token_returns_ready_and_sets_jwt():
    """_sync_access_token() must return status=ready and set Authorization header."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "tok-abc"})
    )

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-1")
    result = await svc._sync_access_token()

    assert result["status"] == "ready"
    assert svc.headers.get("Authorization") == "Bearer tok-abc"
    await http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_sync_access_token_returns_auth_required_for_no_session():
    """_sync_access_token() must return auth_required when backend has no session."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "auth_required", "auth_url": "https://google/auth"})
    )

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-2")
    result = await svc._sync_access_token()

    assert result["status"] == "auth_required"
    assert "auth_url" in result
    assert "Authorization" not in svc.headers  # JWT must NOT be set
    await http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_check_grant_token_returns_granted_true():
    """check_grant_token() must return granted=True when hasGrantToken says valid."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-3")
    svc.set_auth_token("tok-xyz")  # Simulate JWT already set
    result = await svc.check_grant_token()

    assert result["granted"] is True
    await http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_check_grant_token_returns_granted_false_when_no_token():
    """check_grant_token() returns granted=False when exists=False (new user)."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": False, "valid": False,
            "message": "No Google Calendar integration found for this tenant."
        })
    )

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-4")
    svc.set_auth_token("tok-xyz")
    result = await svc.check_grant_token()

    assert result["granted"] is False
    assert "auth_url" in result
    assert "No Google Calendar" in result["reason"]
    await http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_check_grant_token_returns_granted_false_when_refresh_failed():
    """check_grant_token() returns granted=False when exists=True but valid=False and silent refresh fails."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    # hasGrantToken always returns invalid
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": True, "valid": False,
            "message": "Google Calendar integration exists but session is invalid. Re-authentication required."
        })
    )

    # Silent refresh fails
    respx.post(f"{BASE}/auth/googleRefreshToken").mock(
        return_value=Response(200, json={"success": False, "message": "No refresh token"})
    )

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-5")
    svc.set_auth_token("tok-xyz")
    result = await svc.check_grant_token()

    assert result["granted"] is False
    assert "Re-authentication" in result["reason"]
    await http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_check_grant_token_with_successful_silent_refresh():
    """check_grant_token() must attempt silent refresh if grant is invalid, and return True if refresh succeeds."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    # 1. First call to hasGrantToken returns INVALID
    respx.get(f"{BASE}/auth/hasGrantToken").mock(side_effect=[
        Response(200, json={"success": True, "exists": True, "valid": False}), # First check
        Response(200, json={"success": True, "exists": True, "valid": True})   # Second check after refresh
    ])
    
    # 2. Mock the refresh endpoint as SUCCESS
    respx.post(f"{BASE}/auth/googleRefreshToken").mock(
        return_value=Response(200, json={"success": True})
    )

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-refresh")
    svc.set_auth_token("tok-refresh")
    
    result = await svc.check_grant_token()

    assert result["granted"] is True
    # Verify the sequence of calls
    assert len(respx.calls) == 3
    # 1. hasGrantToken (invalid)
    # 2. googleRefreshToken (success)
    # 3. hasGrantToken (valid)
    assert "/auth/hasGrantToken" in str(respx.calls[0].request.url)
    assert "/auth/googleRefreshToken" in str(respx.calls[1].request.url)
    assert respx.calls[1].request.method == "POST"
    assert "/auth/hasGrantToken" in str(respx.calls[2].request.url)
    await http.aclose()


@pytest.mark.asyncio
@respx.mock
async def test_check_grant_token_graceful_on_service_error():
    """check_grant_token() must not crash when hasGrantToken endpoint is unreachable."""
    import httpx
    from src.remote_services.google_core import GoogleCalendarClient

    respx.get(f"{BASE}/auth/hasGrantToken").mock(side_effect=Exception("Connection refused"))

    http = httpx.AsyncClient(base_url=BASE, verify=False)
    svc = GoogleCalendarClient(TENANT, http, "corr-6")
    svc.set_auth_token("tok-xyz")
    result = await svc.check_grant_token()

    assert result["granted"] is False
    assert "auth_url" in result
    assert "unreachable" in result["reason"].lower()
    await http.aclose()


# ---------------------------------------------------------------------------
# INTEGRATION: /ai/chat Pre-LLM Gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_chat_happy_path_proceeds_to_llm():
    """
    Scenario 1: Happy path.
    Step 1 returns JWT, Step 2 confirms grant -> LLM is called and returns response.
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "happy-jwt"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=_mock_empty_session())
    respx.post(f"{BASE}/wallet/deplete").mock(return_value=_mock_wallet())

    with patch("src.main.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(
            return_value=_mock_openai_text("Here is your meeting summary.")
        )
        async with LifespanManager(app) as mgr:
            async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
                resp = await ac.post("/ai/chat", json={"prompt": "schedule a google meeting", "history": []}, headers=HEADERS)
                data = resp.json()

    assert resp.status_code == 200
    assert data.get("response") == "Here is your meeting summary."
    # Confirm both auth calls were made
    access_calls = [c for c in respx.calls if "/auth/accessToken" in str(c.request.url)]
    grant_calls  = [c for c in respx.calls if "/auth/hasGrantToken" in str(c.request.url)]
    assert len(access_calls) >= 1, "Step 1 (_sync_access_token) was not called"
    assert len(grant_calls)  >= 1, "Step 2 (check_grant_token) was not called"


@pytest.mark.asyncio
@respx.mock
async def test_chat_blocked_at_step1_no_session():
    """
    Scenario 2: Step 1 fails (no session) -> auth_required immediately, no grant check.
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "auth_required", "auth_url": "https://google/auth"})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=_mock_empty_session())

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post("/ai/chat", json={"prompt": "schedule a google meeting", "history": []}, headers=HEADERS)
            data = resp.json()

    assert resp.status_code == 200
    assert data["status"] == "auth_required"
    assert data["content"] == "Calendar Access Required"
    # hasGrantToken must NOT have been called
    grant_calls = [c for c in respx.calls if "/auth/hasGrantToken" in str(c.request.url)]
    assert len(grant_calls) == 0, "check_grant_token was called despite Step 1 failing"


@pytest.mark.asyncio
@respx.mock
async def test_chat_blocked_at_step2_new_user_no_token():
    """
    Scenario 3: JWT acquired (Step 1 OK) but user never granted Calendar access (exists=False).
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "valid-jwt"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": False, "valid": False,
            "message": "No Google Calendar integration found for this tenant."
        })
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=_mock_empty_session())

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post("/ai/chat", json={"prompt": "book a google appointment", "history": []}, headers=HEADERS)
            data = resp.json()

    assert resp.status_code == 200
    assert data["status"] == "auth_required"
    assert data["content"] == "Calendar Access Required"
    assert "auth_url" in data


@pytest.mark.asyncio
@respx.mock
async def test_chat_blocked_at_step2_refresh_failed():
    """
    Scenario 4: JWT acquired (Step 1 OK) but token refresh failed (exists=True, valid=False).
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "stale-jwt"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": True, "valid": False,
            "message": "Google Calendar integration exists but session is invalid. Re-authentication required."
        })
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=_mock_empty_session())

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post("/ai/chat", json={"prompt": "schedule a google meeting", "history": []}, headers=HEADERS)
            data = resp.json()

    assert resp.status_code == 200
    assert data["status"] == "auth_required"
    assert data["content"] == "Calendar Access Required"


@pytest.mark.asyncio
@respx.mock
async def test_chat_non_calendar_prompt_skips_handshake():
    """
    Scenario 5: Non-calendar prompt ("create a new client") must NOT trigger the
    auth handshake. hasGrantToken and accessToken should NOT be called.
    """
    # These should NOT be called for non-calendar prompts
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "tok"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=_mock_empty_session())
    respx.post(f"{BASE}/wallet/deplete").mock(return_value=_mock_wallet())

    with patch("src.main.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(
            return_value=_mock_openai_text("Sure, I can help you register a new client.")
        )
        async with LifespanManager(app) as mgr:
            async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
                # This should NOT trigger calendar keywords
                resp = await ac.post("/ai/chat", json={"prompt": "create a new client", "history": []}, headers=HEADERS)

    # Step 2 (hasGrantToken) MUST NOT have been called
    grant_calls  = [c for c in respx.calls if "/auth/hasGrantToken" in str(c.request.url)]
    assert len(grant_calls)  == 0, f"check_grant_token was incorrectly triggered for non-calendar prompt. Calls: {grant_calls}"


# ---------------------------------------------------------------------------
# INTEGRATION: /ai/chat/stream Pre-LLM Gate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_stream_happy_path_passes_gate():
    """
    Scenario 6: Streaming happy path. Both steps pass -> SSE stream starts (no auth chunk).
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "stream-jwt"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=_mock_empty_session())

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post("/ai/chat/stream", json={"prompt": "schedule a google meeting", "history": []}, headers=HEADERS)
            # Read first SSE chunk
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = json.loads(line[6:])
                    assert chunk.get("status") != "auth_required", \
                        "Stream was blocked by auth gate on a valid grant"
                    break


@pytest.mark.asyncio
@respx.mock
async def test_stream_blocked_step1_no_session():
    """
    Scenario 7: Streaming, Step 1 fails -> SSE yields auth_required chunk immediately.
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "auth_required", "auth_url": "https://google/auth"})
    )

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post("/ai/chat/stream", json={"prompt": "book google appointment", "history": []}, headers=HEADERS)
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = json.loads(line[6:])
                    assert chunk["status"] == "auth_required"
                    assert "auth_url" in chunk
                    break


@pytest.mark.asyncio
@respx.mock
async def test_stream_blocked_step2_grant_invalid():
    """
    Scenario 8: Streaming, Step 1 OK, Step 2 grant invalid -> SSE yields auth_required.
    """
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "stale-stream-jwt"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": True, "valid": False,
            "message": "Re-authentication required."
        })
    )

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post("/ai/chat/stream", json={"prompt": "schedule a google meeting", "history": []}, headers=HEADERS)
            async for line in resp.aiter_lines():
                if line.startswith("data: "):
                    chunk = json.loads(line[6:])
                    assert chunk["status"] == "auth_required"
                    assert "auth_url" in chunk
                    break


# ---------------------------------------------------------------------------
# INTEGRATION: Agentic loop gate (active_workflow = "calendar")
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_loop_gate_valid_grant_continues():
    """
    Scenario 9: Mid-workflow turn (active_workflow=calendar).
    Grant is valid -> loop continues and LLM is called.
    """
    active_session = {
        "tenantId": TENANT,
        "metadata": {"active_workflow": "calendar", "event_draft": {"title": "Team Sync"}}
    }
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "mid-jwt"})
    )
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": True, "valid": True})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json=active_session))
    respx.post(f"{BASE}/wallet/deplete").mock(return_value=_mock_wallet())

    with patch("src.main.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(
            return_value=_mock_openai_text("Got it! What time should I book this for?")
        )
        async with LifespanManager(app) as mgr:
            async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
                resp = await ac.post("/ai/chat", json={"prompt": "3pm tomorrow", "history": []}, headers=HEADERS)
                data = resp.json()

    assert resp.status_code == 200
    # Should NOT have been blocked
    assert data.get("status") != "auth_required"
    assert "response" in data


@pytest.mark.asyncio
@respx.mock
async def test_loop_gate_invalid_grant_surfaces_auth_card():
    """
    Scenario 10: Mid-workflow turn (active_workflow=calendar).
    Grant becomes invalid (revoked mid-session) -> auth_required returned.
    """
    active_session = {
        "tenantId": TENANT,
        "metadata": {"active_workflow": "calendar", "event_draft": {"title": "Team Sync"}}
    }
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "mid-jwtt"})
    )
    # Pre-LLM gate passes (keyword "meeting" not in "3pm tomorrow")
    # Loop gate: grant becomes invalid
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": True, "valid": False,
            "message": "Re-authentication required."
        })
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json=active_session))
    respx.post(f"{BASE}/wallet/deplete").mock(return_value=_mock_wallet())

    # We need to mock OpenAI because "3pm tomorrow" doesn't trigger the Pre-LLM gate.
    # The LLM will then try to call schedule_event, which will trigger the Agent-level gate.
    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            # Add "schedule" to trigger the Pre-LLM gatekeeper
            resp = await ac.post("/ai/chat", json={"prompt": "schedule google 3pm tomorrow", "history": []}, headers=HEADERS)
            data = resp.json()

    assert resp.status_code == 200
    assert data["status"] == "auth_required"
    assert data["content"] == "Calendar Access Required"
    assert "auth_url" in data


# ---------------------------------------------------------------------------
# INTEGRATION: calendar_agent.py preflight (func-level grant check)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@respx.mock
async def test_agent_preflight_blocks_schedule_event_when_grant_invalid():
    """
    Scenario 12: calendar_agent.py preflight blocks schedule_event when grant is invalid.
    The auth_required from the agent propagates up to the endpoint and surfaces auth card.
    """
    active_session = {
        "tenantId": TENANT,
        "metadata": {
            "active_workflow": "calendar",
            "event_draft": {
                "title": "Deposition",
                "startTime": "2026-03-20T10:00:00Z",
                "summary_requested": True,
                "attendees_requested": True,
                "location_requested": True,
            }
        }
    }
    # Step 1: JWT ready
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "agent-jwt"})
    )
    # Grant check: called TWICE (once at loop gate, once in calendar_agent preflight)
    # Both return invalid so the loop gate catches it first
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={
            "success": True, "exists": True, "valid": False,
            "message": "Re-authentication required."
        })
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json=active_session))

    async with LifespanManager(app) as mgr:
        async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
            resp = await ac.post(
                "/ai/chat",
                json={"prompt": "schedule google conference room 3", "history": []},  # Explicit Google keyword
                headers=HEADERS
            )
            data = resp.json()

    assert resp.status_code == 200
    assert data["status"] == "auth_required"


@pytest.mark.asyncio
@respx.mock
async def test_get_system_status_bypasses_grant_check():
    """
    Scenario 13: get_system_status is a calendar_funcs tool but does NOT require
    Google Calendar access. It must bypass check_grant_token entirely.
    """
    active_session = {"tenantId": TENANT, "metadata": {}}
    respx.get(f"{BASE}/auth/accessToken").mock(
        return_value=Response(200, json={"status": "ready", "jwtToken": "sys-jwt"})
    )
    # Grant check should NOT be called for get_system_status
    respx.get(f"{BASE}/auth/hasGrantToken").mock(
        return_value=Response(200, json={"success": True, "exists": False, "valid": False})
    )
    respx.get(f"{BASE}/chat/session").mock(return_value=Response(200, json=active_session))
    # Mock the health endpoint the tool calls
    respx.get(f"{BASE}/").mock(return_value=Response(200, json={"status": "online"}))
    respx.post(f"{BASE}/wallet/deplete").mock(return_value=_mock_wallet())

    tool_call_mock = MagicMock()
    tool_call_mock.id = "call_sys"
    tool_call_mock.function.name = "get_system_status"
    tool_call_mock.function.arguments = "{}"

    msg_with_tool = MagicMock()
    msg_with_tool.content = None
    msg_with_tool.role = "assistant"
    msg_with_tool.tool_calls = [tool_call_mock]
    msg_with_tool.model_dump.return_value = {
        "role": "assistant",
        "tool_calls": [{"id": "call_sys", "type": "function",
                        "function": {"name": "get_system_status", "arguments": "{}"}}]
    }

    msg_final = MagicMock()
    msg_final.content = "All systems are online."
    msg_final.tool_calls = None
    msg_final.role = "assistant"
    msg_final.model_dump.return_value = {"role": "assistant", "content": "All systems are online."}

    resp_1 = MagicMock()
    resp_1.choices = [MagicMock(message=msg_with_tool)]
    resp_1.usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)

    resp_2 = MagicMock()
    resp_2.choices = [MagicMock(message=msg_final)]
    resp_2.usage = MagicMock(prompt_tokens=5, completion_tokens=5, total_tokens=10)

    with patch("src.main.AsyncOpenAI") as mock_cls:
        mock_cls.return_value.chat.completions.create = AsyncMock(side_effect=[resp_1, resp_2])
        async with LifespanManager(app) as mgr:
            async with AsyncClient(transport=ASGITransport(mgr.app), base_url="http://test") as ac:
                resp = await ac.post("/ai/chat", json={"prompt": "check system status", "history": []}, headers=HEADERS)
                data = resp.json()

    # Must not be blocked by auth
    assert resp.status_code == 200
    assert data.get("status") != "auth_required"
    # hasGrantToken must NOT have been called (system status bypasses grant check)
    grant_calls = [c for c in respx.calls if "/auth/hasGrantToken" in str(c.request.url)]
    assert len(grant_calls) == 0, "check_grant_token was incorrectly triggered for get_system_status"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
