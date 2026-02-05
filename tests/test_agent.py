import pytest
import respx
from httpx import AsyncClient, ASGITransport, Response
from src.main import app

# Mark all tests in this file as async
pytestmark = pytest.mark.asyncio

@respx.mock
async def test_health_endpoint():
    """Verify the health check logic using the new Transport syntax."""
    
    # We mock the remote backend call
    respx.get("https://dev.matterminer.com/calendar").mock(return_value=Response(200, json={"message": "ok"}))
    
    # --- FIXED LINE BELOW ---
    # We use ASGITransport to link the client to our FastAPI app
    transport = ASGITransport(app=app)
    
    async with AsyncClient(transport=transport, base_url="https://dev.matterminer.com/calendar") as ac:
        response = await ac.get("/")
        assert response.status_code == 200
        assert response.json()["status"] == "online"

@respx.mock
async def test_tenant_header_required():
    """Verify missing headers cause a 422 error."""
    transport = ASGITransport(app=app)
    
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        response = await ac.post("/ai/chat", json={"prompt": "Hello"})
        assert response.status_code == 422