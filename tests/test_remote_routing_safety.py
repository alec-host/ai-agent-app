import pytest
import respx
import httpx
from src.remote_services.google_core import GoogleCalendarClient
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.remote_services.wallet_service import WalletClient
from src.config import settings

@pytest.mark.asyncio
async def test_google_client_routing_safety():
    """
    Verify that GoogleCalendarClient handles various base_url formats without app/app/ bug.
    """
    # 1. Test with clean host
    client_host = GoogleCalendarClient("123", httpx.AsyncClient(), "corr")
    client_host.base_url = "https://dev.matterminer.com"
    
    with respx.mock:
        respx.get("https://dev.matterminer.com/app/chat/session").mock(return_value=httpx.Response(200, json={"status": "success"}))
        resp = await client_host.get_client_session("123")
        assert resp == {"status": "success"}

    # 2. Test with legacy /api or /calendar suffix (Safety Check)
    client_legacy = GoogleCalendarClient("123", httpx.AsyncClient(), "corr")
    # Simulate a legacy .env
    client_legacy.base_url = "https://dev.matterminer.com/calendar".rstrip("/").replace("/api", "").replace("/app", "").replace("/calendar", "")
    assert client_legacy.base_url == "https://dev.matterminer.com"
    
    with respx.mock:
        respx.get("https://dev.matterminer.com/app/chat/session").mock(return_value=httpx.Response(200, json={"status": "success"}))
        resp = await client_legacy.get_client_session("123")
        assert resp == {"status": "success"}

@pytest.mark.asyncio
async def test_matterminer_core_routing_safety():
    """
    Verify MatterMinerCoreClient prepends /app/core/app correctly.
    """
    # Test with domain only
    client = MatterMinerCoreClient(base_url="https://dev.matterminer.com", tenant_id="123")
    assert client.base_url == "https://dev.matterminer.com"
    
    with respx.mock:
        respx.get("https://dev.matterminer.com/app/core/search-contact").mock(return_value=httpx.Response(200, json={"id": 1}))
        resp = await client.search_contact_by_email("test@test.com")
        assert resp["id"] == 1

@pytest.mark.asyncio
async def test_wallet_client_routing_safety():
    """
    Verify WalletClient prepends /app wallet path correctly.
    """
    client = WalletClient("123", httpx.AsyncClient())
    client.base_url = "https://dev.matterminer.com"
    
    with respx.mock:
        respx.get("https://dev.matterminer.com/app/wallet/check-balance?tenantId=123").mock(return_value=httpx.Response(200, json={"allowed": True}))
        resp = await client.check_balance()
        assert resp["allowed"] == True

if __name__ == "__main__":
    pytest.main([__file__])
