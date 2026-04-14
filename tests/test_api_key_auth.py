"""
test_api_key_auth.py — Phase 6 (Auth Migration)

Comprehensive test suite verifying the API key replaces login for all Core operations.
Tests the transport layer, error taxonomy, tool surface removal, and workflow continuity.
"""
import pytest
import respx
import httpx
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.config import settings
from src.tools import TOOLS


class TestCoreAPIKeyTransport:
    """Phase 1 verification: API key is injected into every outbound Core request."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_key_present_in_get_request(self):
        """GET requests include the Authorization header."""
        respx.get(url__regex=r".*/countries.*").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        await client.get_countries()
        
        auth = respx.calls[0].request.headers.get("authorization")
        assert auth == f"Bearer {settings.CORE_API_KEY}"
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_api_key_present_in_post_request(self):
        """POST requests include the Authorization header."""
        respx.post(url__regex=r".*/contact.*").mock(
            return_value=httpx.Response(200, json={"status": "success"})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        await client.create_contact({"first_name": "Test"})
        
        auth = respx.calls[0].request.headers.get("authorization")
        assert auth == f"Bearer {settings.CORE_API_KEY}"
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_tenant_header_still_present(self):
        """X-Tenant-ID must still be included alongside the API key."""
        respx.get(url__regex=r".*/countries.*").mock(
            return_value=httpx.Response(200, json={"status": "success", "data": []})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "my-tenant")
        await client.get_countries()
        
        headers = dict(respx.calls[0].request.headers)
        assert headers["x-tenant-id"] == "my-tenant"
        assert "authorization" in headers
        await client.close()

    @pytest.mark.asyncio
    async def test_no_access_token_attribute(self):
        """MatterMinerCoreClient must not have access_token, login, etc."""
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        assert not hasattr(client, "access_token")
        assert not hasattr(client, "user_profile")
        assert not hasattr(client, "login")
        assert not hasattr(client, "has_valid_token")
        assert not hasattr(client, "set_auth_token")
        assert not hasattr(client, "is_authenticated")
        await client.close()


class TestCoreAPIKeyErrorTaxonomy:
    """Phase 1 verification: 401/403 → api_key_error, 404 → normal error."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_401_returns_api_key_error(self):
        """Core 401 → api_key_error."""
        respx.get(url__regex=r".*/countries.*").mock(
            return_value=httpx.Response(401, json={"message": "Unauthorized"})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        resp = await client.get_countries()
        assert resp["status"] == "api_key_error"
        assert resp["code"] == 401
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_403_returns_api_key_error(self):
        """Core 403 → api_key_error."""
        respx.post(url__regex=r".*/contact.*").mock(
            return_value=httpx.Response(403, json={"message": "Forbidden"})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        resp = await client.create_contact({"first_name": "Test"})
        assert resp["status"] == "api_key_error"
        assert resp["code"] == 403
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_404_is_data_error_not_auth(self):
        """Core 404 → error, NOT api_key_error or auth_required."""
        respx.get(url__regex=r".*/countries.*").mock(
            return_value=httpx.Response(404, json={"message": "Not found"})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        resp = await client.get_countries()
        assert resp["status"] == "error"
        assert resp["code"] == 404
        await client.close()

    @pytest.mark.asyncio
    @respx.mock
    async def test_500_is_server_error(self):
        """Core 500 → error."""
        respx.get(url__regex=r".*/countries.*").mock(
            return_value=httpx.Response(500, json={"message": "Internal error"})
        )
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        resp = await client.get_countries()
        assert resp["status"] == "error"
        assert resp["code"] == 500
        await client.close()


class TestToolSurfaceRemoval:
    """Phase 3 verification: authenticate_to_core tool is fully removed."""

    def test_authenticate_to_core_not_in_tools(self):
        """The TOOLS list must NOT contain authenticate_to_core."""
        names = [t["function"]["name"] for t in TOOLS]
        assert "authenticate_to_core" not in names

    def test_no_password_parameters_in_tools(self):
        """No tool in TOOLS should have a 'password' parameter."""
        for tool in TOOLS:
            params = tool.get("function", {}).get("parameters", {})
            props = params.get("properties", {})
            assert "password" not in props, \
                f"Tool '{tool['function']['name']}' still has a password parameter"


class TestAPIKeyConfigValidation:
    """Edge case: CORE_API_KEY must exist in settings."""

    def test_core_api_key_defined_in_settings(self):
        """settings.CORE_API_KEY must be a non-empty string."""
        assert hasattr(settings, "CORE_API_KEY")
        assert isinstance(settings.CORE_API_KEY, str)
        assert len(settings.CORE_API_KEY) > 0

    @pytest.mark.asyncio
    async def test_api_key_not_exposed_in_error_messages(self):
        """The actual API key value must not appear in error response messages."""
        client = MatterMinerCoreClient("https://dev.matterminer.com", "t1")
        # Simulate an api_key_error response
        with respx.mock:
            respx.get(url__regex=r".*/countries.*").mock(
                return_value=httpx.Response(401, json={"message": "Unauthorized"})
            )
            resp = await client.get_countries()
            assert settings.CORE_API_KEY not in resp.get("message", "")
        await client.close()
