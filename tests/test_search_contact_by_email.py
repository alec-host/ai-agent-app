import pytest
import respx
import httpx
from src.config import settings
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.agents.core_agent import handle_search_contact

CONTACT_FOUND_RESPONSE = {
    "status": "success",
    "contact_id": 9999
}

CONTACT_NOT_FOUND_RESPONSE = {
    "status": "error",
    "message": "Contact not found",
    "code": 404
}

AUTH_REQUIRED_RESPONSE = {
    "status": "error",
    "message": "Not found", # This triggers MatterMinerCoreClient's 404 session expiration check
    "success": False
}

BASE_URL = settings.NODE_REMOTE_SERVICE_URL.rstrip('/')
ENDPOINT = f"{BASE_URL}/search-contact"

@pytest.mark.asyncio
@respx.mock
async def test_core_client_search_contact_by_email():
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=CONTACT_FOUND_RESPONSE)
    )
    
    client = MatterMinerCoreClient(base_url=BASE_URL, tenant_id="test_tenant")
    resp = await client.search_contact_by_email("test@example.com")
    
    assert resp["status"] == "success"
    assert resp["contact_id"] == 9999
    await client.close()

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_found():
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=CONTACT_FOUND_RESPONSE)
    )
    
    args = {"email": "test@example.com"}
    result = await handle_search_contact(args, tenant_id="test_tenant")
    
    assert result["status"] == "success"
    assert "Contact found! ID is 9999" in result["message"]
    assert "9999" in result["response_instruction"]

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_not_found():
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(404, json=CONTACT_NOT_FOUND_RESPONSE)
    )
    
    args = {"email": "nobody@example.com"}
    result = await handle_search_contact(args, tenant_id="test_tenant")
    
    assert result["status"] == "not_found"
    assert "No contact found" in result["message"]
    assert "create a new contact instead" in result["response_instruction"]

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_auth_required():
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(404, json=AUTH_REQUIRED_RESPONSE)
    )
    
    args = {"email": "test@example.com"}
    result = await handle_search_contact(args, tenant_id="test_tenant")
    
    assert result["status"] == "auth_required"
    assert result["auth_type"] == "matterminer_core"
    assert "Display the login card" in result["response_instruction"]

@pytest.mark.asyncio
async def test_agent_handle_search_contact_missing_email():
    args = {}
    result = await handle_search_contact(args, tenant_id="test_tenant")
    
    assert result["status"] == "error"
    assert "Email address is required" in result["message"]
