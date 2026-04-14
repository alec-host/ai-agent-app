import pytest
import respx
import json
import httpx
from src.agents.core_agent import handle_search_contact, handle_lookup_countries, handle_core_ops, handle_create_client
from src.config import settings
from unittest.mock import AsyncMock, MagicMock

BASE_URL = settings.NODE_REMOTE_SERVICE_URL.rstrip('/')

@pytest.fixture
def mock_services():
    services = MagicMock()
    calendar_service = AsyncMock()
    # State mock to track metadata changes
    session_state = {"metadata": {}}
    
    async def get_session(t, user_email=None):
        return session_state
        
    async def sync_session(p):
        nonlocal session_state
        # Merge top-level metadata from payload if present
        if "metadata" in p:
            session_state["metadata"].update(p["metadata"])
        # Also handle explicit client_draft in payload
        if "client_draft" in p:
            if "client_draft" not in session_state["metadata"]:
                session_state["metadata"]["client_draft"] = {}
            session_state["metadata"]["client_draft"].update(p["client_draft"])
        return True

    calendar_service.get_client_session = get_session
    calendar_service.sync_client_session = sync_session
    calendar_service.thread_id = "test-thread"
    services.__getitem__.return_value = calendar_service
    return services, session_state

@pytest.mark.asyncio
@respx.mock
async def test_pattern_search_contact_links_to_client(mock_services):
    services, state = mock_services
    endpoint = f"{BASE_URL}/search-contact"
    
    respx.get(url__regex=r".*/search-contact.*").mock(return_value=httpx.Response(200, json={
        "status": "success",
        "contact_id": "found-cont-999"
    }))
    
    args = {"email": "lookup@example.com"}
    await handle_search_contact(args, services, tenant_id="test", user_email="agent@test.com")
    
    # Verify that contact_id was linked to the client_draft in the session state
    client_draft = state["metadata"].get("client_draft", {})
    assert client_draft.get("contact_id") == "found-cont-999"

@pytest.mark.asyncio
@respx.mock
async def test_pattern_lookup_country_links_to_client(mock_services):
    services, state = mock_services
    endpoint = f"{BASE_URL}/countries"
    
    # Mock unique country match
    respx.get(url__regex=r".*/countries.*").mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": [{"id": 42, "name": "Wakanda"}]
    }))
    
    args = {"search": "Wakanda"}
    await handle_lookup_countries(args, services, tenant_id="test", user_email="test@example.com")
    
    # Verify that country_id was linked
    client_draft = state["metadata"].get("client_draft", {})
    assert client_draft.get("country_id") == 42

@pytest.mark.asyncio
@respx.mock
async def test_pattern_lookup_country_multiple_matches_no_link(mock_services):
    services, state = mock_services
    endpoint = f"{BASE_URL}/countries"
    
    # Mock multiple country matches
    respx.get(url__regex=r".*/countries.*").mock(return_value=httpx.Response(200, json={
        "status": "success",
        "data": [
            {"id": 1, "name": "USA"},
            {"id": 2, "name": "USA Minor Outlying Islands"}
        ]
    }))
    
    args = {"search": "USA"}
    await handle_lookup_countries(args, services, tenant_id="test", user_email="test@example.com")
    
    # Verify that NO country_id was linked because it wasn't unique
    client_draft = state["metadata"].get("client_draft", {})
    assert "country_id" not in client_draft

@pytest.mark.asyncio
@respx.mock
async def test_pattern_promote_tool_uses_client_logic(mock_services):
    # This tests that handle_core_ops routes promote_contact_to_client to handle_create_client
    services, state = mock_services
    endpoint = f"{BASE_URL}/client"
    
    respx.post(url__regex=r".*/client.*").mock(return_value=httpx.Response(200, json={"status": "success"}))
    
    # Populate the "missing" fields in metadata so it hits the final save
    state["metadata"]["client_draft"] = {
        "first_name": "John",
        "last_name": "Doe",
        "client_email": "john@doe.com"
    }
    
    args = {
        "contact_id": "existing-cont-1",
        "client_type": "individual",
        "country_id": 5,
        "street": "1 Main St"
    }
    
    # Call core_ops instead of handle_create_client directly to test routing
    result = await handle_core_ops("promote_contact_to_client", args, services, tenant_id="test", history=[], user_email="test@example.com")
    
    assert result["status"] == "success"
    assert "Successfully registered client" in result["message"]
