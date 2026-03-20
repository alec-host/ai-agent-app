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

from unittest.mock import AsyncMock, MagicMock

@pytest.fixture
def mock_services():
    services = MagicMock()
    calendar_service = AsyncMock()
    # Mocking get_client_session to return a dict with metadata
    calendar_service.get_client_session.return_value = {"metadata": {}}
    calendar_service.thread_id = "test_thread"
    services.__getitem__.return_value = calendar_service
    return services

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_found(mock_services):
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json=CONTACT_FOUND_RESPONSE)
    )
    
    args = {"email": "test@example.com"}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    
    assert result["status"] == "success"
    assert "Contact found! ID is 9999" in result["message"]
    # Verify persistence call
    mock_services['calendar'].sync_client_session.assert_called()

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_not_found(mock_services):
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(404, json=CONTACT_NOT_FOUND_RESPONSE)
    )
    
    args = {"email": "nobody@example.com"}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    
    assert result["status"] == "not_found"
    assert "No contact found" in result["message"]

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_auth_required(mock_services):
    respx.get(ENDPOINT).mock(
        return_value=httpx.Response(404, json=AUTH_REQUIRED_RESPONSE)
    )
    
    args = {"email": "test@example.com"}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    
    assert result["status"] == "auth_required"

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_nested_found(mock_services):
    nested_response = {"status": "success", "data": {"contact_id": 8888}}
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json=nested_response))
    
    args = {"email": "nested@example.com"}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    
    assert result["status"] == "success"
    assert "8888" in result["message"]

@pytest.mark.asyncio
async def test_agent_handle_search_contact_missing_email(mock_services):
    args = {}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    assert result["status"] == "error"

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_server_error(mock_services):
    respx.get(ENDPOINT).mock(return_value=httpx.Response(500, json={}))
    args = {"email": "broken@example.com"}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    assert result["status"] == "error"

@pytest.mark.asyncio
@respx.mock
async def test_agent_handle_search_contact_200_empty_response(mock_services):
    respx.get(ENDPOINT).mock(return_value=httpx.Response(200, json={"status": "success"}))
    args = {"email": "nothing@example.com"}
    result = await handle_search_contact(args, mock_services, tenant_id="test_tenant")
    assert result["status"] == "not_found"
