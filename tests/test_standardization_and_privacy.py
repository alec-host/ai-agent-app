import pytest
import json
import asyncio
import respx
from httpx import Response
from src.utils import standardize_response, sanitize_history
from src.agent_manager import execute_tool_call

@pytest.mark.asyncio
async def test_standardization_logic():
    """
    Verify that standardize_response always injects 'response' and 'history'.
    """
    # Case 1: Payload with 'message' and no 'history'
    payload = {"status": "auth_required", "message": "Login required"}
    std = standardize_response(payload)
    assert "response" in std
    assert std["response"] == "Login required"
    assert "history" in std
    assert isinstance(std["history"], list)

    # Case 2: Payload with 'content' and no 'history'
    payload = {"content": "Hello world"}
    std = standardize_response(payload)
    assert std["response"] == "Hello world"
    assert std["history"] == []

    # Case 3: Payload already has everything
    payload = {"response": "Fixed", "history": [{"role": "user", "content": "hi"}]}
    std = standardize_response(payload)
    assert std["response"] == "Fixed"
    assert len(std["history"]) == 1

@pytest.mark.asyncio
async def test_privacy_redaction_literal():
    """
    Verify that sanitize_history redacts literal values (Email/TenantID).
    """
    user_email = "secret@lawfirm.com"
    tenant_id = "tenant-uuid-123"
    history = [
        {"role": "user", "content": f"My email is {user_email}"},
        {"role": "assistant", "content": f"Confirmed for {tenant_id}"}
    ]
    
    sanitized = sanitize_history(history, redact_values=[user_email, tenant_id])
    
    assert "[REDACTED]" in sanitized[0]["content"]
    assert user_email not in sanitized[0]["content"]
    assert "[REDACTED]" in sanitized[1]["content"]
    assert tenant_id not in sanitized[1]["content"]

@pytest.mark.asyncio
async def test_security_token_masking():
    """
    Verify that sensitize_history masks keys like jwtToken and password.
    """
    history = [
        {"role": "assistant", "content": '{"jwtToken": "super-secret-token", "password": "123"}'}
    ]
    
    sanitized = sanitize_history(history)
    assert "********" in sanitized[0]["content"]
    assert "super-secret-token" not in sanitized[0]["content"]

    # Test tool_calls arguments masking
    history = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_std_1",
                "function": {
                    "name": "lookup_countries",
                    "arguments": '{"search": "US", "password": "secret-password"}'
                }
            }]
        },
        {
            "role": "tool",
            "tool_call_id": "call_std_1",
            "content": '{"success": true}'
        }
    ]
    sanitized = sanitize_history(history)
    # The assistant message is at index 0
    args_str = sanitized[0]["tool_calls"][0]["function"]["arguments"]
    assert "********" in args_str
    assert "secret-password" not in args_str

@pytest.mark.asyncio
async def test_dispatcher_redaction():
    """
    Verify that execute_tool_call's internal redactor works on tool results.
    """
    user_email = "owner@matterminer.com"
    tenant_id = "firm-777"
    
    # Mock tool call
    class MockTool:
        def __init__(self):
            self.function = type('obj', (object,), {"name": "initialize_calendar_session", "arguments": "{}"})
            self.id = "call_1"

    # Mock services
    class MockCalendar:
        def __init__(self):
            self.thread_id = "t1"
            self.base_url = "http://node"
        async def sync_client_session(self, p): return True
        async def get_client_session(self, t): return {"status": "success", "data": {"metadata": {}}}
        def is_authenticated(self): return True
        def set_auth_token(self, t, is_jwt=False): None
        async def _sync_access_token(self): return {"status": "ready"}
        async def check_grant_token(self): return {"granted": True}

    mock_calendar = MockCalendar()
    
    # Tool handler that returns sensitive data
    async def mock_handle_calendar(*args, **kwargs):
        return {
            "status": "ready",
            "jwtToken": "token123",
            "info": f"Account owned by {user_email} in {tenant_id}"
        }

    import src.agent_manager
    src.agent_manager.handle_calendar = mock_handle_calendar

    result = await execute_tool_call(MockTool(), {"calendar": mock_calendar}, "admin", tenant_id, [], user_email=user_email)
    
    # If the dispatcher failed internally, 'result' might be an error dict.
    # Check for success first (status: ready)
    assert result.get("status") == "ready"
    
    # 1. jwtToken should be POPPED (gone)
    assert "jwtToken" not in result
    # 2. user_email should be REDACTED in 'info'
    assert user_email not in result.get("info", "")
    assert "[REDACTED]" in result.get("info", "")
    # 3. tenant_id should be REDACTED
    assert tenant_id not in result.get("info", "")

if __name__ == "__main__":
    pytest.main([__file__])
