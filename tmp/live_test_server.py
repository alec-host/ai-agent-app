import os
import uvicorn
import respx
import json
from httpx import Response
from unittest.mock import AsyncMock, MagicMock, patch

# Set dummy key for config
os.environ["OPENAI_API_KEY"] = "sk-live-test-key"

from src.main import app
from fastapi.staticfiles import StaticFiles

# MOUNT UI
ui_dir = os.path.join(os.getcwd(), "demo-ui")
app.mount("/demo", StaticFiles(directory=ui_dir), name="demo")

# STARTUP MOCKS
respx_router = respx.mock
respx_router.start()

# Helper for respx mocks
def mock_all_node_endpoints():
    # Session
    respx_router.get(url__regex=r".*/chat/session.*").mock(
        return_value=Response(200, json={
            "tenantId": "12345678",
            "client_number": None,
            "metadata": {"active_workflow": None}
        })
    )
    respx_router.post(url__regex=r".*/chat/session.*").mock(return_value=Response(200, json={"status": "success"}))
    respx_router.delete(url__regex=r".*/chat/session.*").mock(return_value=Response(200, json={"status": "success"}))
    # Auth
    respx_router.get(url__regex=r".*/auth/accessToken.*").mock(return_value=Response(200, json={"status": "ready"}))
    # Wallet
    respx_router.post(url__regex=r".*/wallet/deplete.*").mock(return_value=Response(200, json={"status": "ok"}))

mock_all_node_endpoints()

# AI MOCK CONTENT
table_content = """### FINAL SUMMARY: CLIENT REGISTERED

| Field | Value |
| :--- | :--- |
| **First Name** | Peter |
| **Last Name** | Pan |
| **Email** | peter@ypmail.com |
"""

# Patching AsyncOpenAI.chat.completions.create globally
async def mock_chat_create(*args, **kwargs):
    # This return is what OpenAI's SDK expects
    mock_resp = MagicMock()
    # Choice object
    choice = MagicMock()
    msg = MagicMock()
    msg.content = table_content
    msg.tool_calls = None
    msg.role = "assistant"
    msg.model_dump.return_value = {"role": "assistant", "content": table_content}
    choice.message = msg
    
    mock_resp.choices = [choice]
    mock_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    
    return mock_resp

if __name__ == "__main__":
    print(f"--- LIVE TEST SERVER STARTING ON http://127.0.0.1:8000/demo/index.html ---")
    
    # Corrected patch path for 1.x SDK
    with patch("openai.resources.chat.completions.AsyncCompletions.create", side_effect=mock_chat_create):
        uvicorn.run(app, host="127.0.0.1", port=8000)
