"""
Phase 6 (Auth Migration): Rewritten test_404_auth_trigger.py

BEFORE: 404 "Not Found" was treated as an auth_required signal (session expiration).
AFTER:  404 is treated as a normal data error. 401/403 trigger api_key_error.
"""
import pytest
import respx
from httpx import Response
from src.agents.core_agent import handle_create_contact, handle_lookup_countries
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.config import settings

@pytest.mark.asyncio
async def test_404_is_data_error_not_auth_countries():
    """404 from /countries should be a normal error, NOT api_key_error."""
    tenant_id = "test-tenant"

    with respx.mock:
        respx.get(url__regex=r".*/countries.*").mock(return_value=Response(
            404,
            json={"success": False, "message": "Not found"}
        ))

        async def mock_get_session(t, user_email=None):
            return {"metadata": {}}

        mock_service_obj = type('obj', (object,), {
            'get_client_session': mock_get_session,
            'access_token': None,
            'thread_id': "test-thread"
        })
        mock_services = {"calendar": mock_service_obj, "session": mock_service_obj}

        args = {"search": "test"}
        result = await handle_lookup_countries(args, mock_services, tenant_id, user_email="test@example.com")

        # 404 should now be treated as a normal error, not auth_required
        assert result["status"] == "error" or result["status"] == "success", \
            f"404 should not trigger api_key_error, got: {result['status']}"
        assert result.get("status") != "api_key_error", "404 must NOT trigger api_key_error"
        assert result.get("status") != "auth_required", "404 must NOT trigger auth_required (legacy)"


@pytest.mark.asyncio
async def test_401_triggers_api_key_error_countries():
    """401 Unauthorized from /countries should trigger api_key_error."""
    tenant_id = "test-tenant"

    with respx.mock:
        respx.get(url__regex=r".*/countries.*").mock(return_value=Response(
            401,
            json={"success": False, "message": "Unauthorized"}
        ))

        async def mock_get_session(t, user_email=None):
            return {"metadata": {}}

        mock_service_obj = type('obj', (object,), {
            'get_client_session': mock_get_session,
            'access_token': None,
            'thread_id': "test-thread"
        })
        mock_services = {"calendar": mock_service_obj, "session": mock_service_obj}

        args = {"search": "Kenya"}
        result = await handle_lookup_countries(args, mock_services, tenant_id, user_email="test@example.com")

        assert result["status"] == "api_key_error", \
            f"401 should trigger api_key_error, got: {result['status']}"
        assert "administrator" in result.get("response_instruction", "").lower()


@pytest.mark.asyncio
async def test_403_triggers_api_key_error_contact():
    """403 Forbidden during contact creation should trigger api_key_error."""
    tenant_id = "test-tenant"

    with respx.mock:
        respx.post(url__regex=r".*/contact.*").mock(return_value=Response(
            403,
            json={"success": False, "message": "Forbidden"}
        ))

        async def mock_get_session(t, user_email=None):
            return {
                "metadata": {
                    "active_workflow": "contact",
                    "contact_draft": {
                        "first_name": "John",
                        "last_name": "Doe",
                        "client_email": "john@doe.com",
                        "contact_type": "primary",
                        "title": "Mr.",
                        "middle_name": "James",
                        "country_code": "US",
                        "phone_number": "1234567890"
                    }
                }
            }

        async def mock_sync_session(p):
            return None

        mock_service_obj = type('obj', (object,), {
            'get_client_session': mock_get_session,
            'sync_client_session': mock_sync_session,
            'access_token': None,
            'thread_id': "test-thread"
        })
        mock_services = {
            'calendar': mock_service_obj,
            'session': mock_service_obj
        }

        args = {}  # All fields already in draft
        result = await handle_create_contact(args, mock_services, tenant_id, history=[{"role": "user", "content": "gibbs C483838 individual Jane Smith"}], user_email="test@example.com")

        assert result["status"] == "api_key_error", \
            f"403 should trigger api_key_error, got: {result['status']}"
        assert "credentials" in result.get("response_instruction", "").lower() or \
               "administrator" in result.get("response_instruction", "").lower()
        # Must NOT mention login card
        assert "login card" not in result.get("response_instruction", "").lower()
