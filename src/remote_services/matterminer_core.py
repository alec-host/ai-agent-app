import httpx
import logging
from typing import Optional, Dict, Any

from src.config import settings

logger = logging.getLogger("legal-agentic-ai")

class MatterMinerCoreClient:
    """
    Client for interacting with the MatterMiner Core remote system.
    Authentication is handled via a static API key (CORE_API_KEY) injected into
    every outbound request header. No interactive login required.
    """
    def __init__(self, base_url: str, tenant_id: str, user_email: Optional[str] = None, correlation_id: Optional[str] = None):
        # Flexible Base URL: Honor the .env path but ensure clean joining (Architectural Guard)
        self.base_url = settings.NODE_REMOTE_SERVICE_URL.rstrip("/")
        # Determine the root prefix (/app or /api) from settings, fallback to /app
        self.root_prefix = "/api" if "/api" in self.base_url.lower() else "/app"
        # Strip the prefix from base_url to prevent double-prefixing in request()
        self.base_url = self.base_url.replace("/api", "").replace("/app", "").replace("/calendar", "").replace("/core", "")
        self.tenant_id = tenant_id
        self.user_email = user_email
        self.correlation_id = correlation_id
        
        # Internal async client
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            verify=settings.TLS_VERIFY  # SEC-07: TLS verification enabled by default
        )



    async def request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Reusable method for calling remote operations.
        Passes tenant information via headers (SEC-05).
        """
        # Routing Logic: Prepends the resolved root prefix and core segment
        url = f"{self.base_url}{self.root_prefix}/core/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        
        logger.info(f"[CORE-API] SUBMITTING {method} TO: {url}")
        
        try:
            response = await self.client.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=headers
            )
            
            # --- API KEY AUTH DETECTION (Phase 1: Auth Migration) ---
            # 401/403 indicate the API key is invalid or lacks permissions.
            # 404 is treated as a normal "data not found" response (no longer conflated with auth).
            if response.status_code in [401, 403]:
                logger.error(
                    f"[CORE-API] API Key rejected for {endpoint}. "
                    f"Status: {response.status_code}. Check CORE_API_KEY configuration."
                )
                return {
                    "status": "api_key_error",
                    "code": response.status_code,
                    "message": "MatterMiner Core rejected the API key. Please contact your administrator."
                }

            # Scalable result handling
            if response.status_code in [200, 201]:
                return response.json()
            else:
                try:
                    error_data = response.json()
                except:
                    error_data = {"message": response.text}
                logger.error(f"[CORE-API] Error {response.status_code} for {endpoint}: {error_data}")
                return {"status": "error", "code": response.status_code, "message": error_data.get("message", "Request failed")}
                
        except Exception as e:
            logger.error(f"[CORE-API] Exception for {endpoint}: {e}")
            return {"status": "error", "message": str(e)}

    async def search_contact_by_email(self, email: str) -> Dict[str, Any]:
        """Searches for a contact by email and returns their contact_id."""
        params = {
            "search": email,
            "tenantId": self.tenant_id
        }
        return await self.request("GET", "/search-contact", params=params)

    async def create_contact(self, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new contact record in MatterMiner Core."""
        # Standardize key mapping: client_email -> email for Core API compatibility
        processed_data = contact_data.copy()
        if "client_email" in processed_data and "email" not in processed_data:
            processed_data["email"] = processed_data.pop("client_email")

        payload = {
            "tenantId": self.tenant_id,
            **processed_data
        }
        logger.info(f"[CONTACT-POST] Payload: {payload}")
        return await self.request("POST", "/contact", json_data=payload)

    async def create_client(self, client_data: Dict[str, Any]) -> Dict[str, Any]:
        """Registers a new client record in MatterMiner Core."""
        # Standardize key mapping: client_email -> email for Core API compatibility
        processed_data = client_data.copy()
        if "client_email" in processed_data and "email" not in processed_data:
            processed_data["email"] = processed_data.pop("client_email")

        payload = {
            "tenantId": self.tenant_id,
            **processed_data
        }
        logger.info(f"[CLIENT-POST] Payload: {payload}")
        return await self.request("POST", "/client", json_data=payload)

    async def create_core_event(self, event_data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a calendar event in the MatterMiner Core system."""
        is_all_day = event_data.get("is_all_day", False)
        
        # Determine the correct routing path
        endpoint = "/all-event" if is_all_day else "/standard-event"
        
        payload = {
            "tenantId": self.tenant_id,
            **event_data
        }
        return await self.request("POST", endpoint, json_data=payload)

    async def get_countries(self, search: str = "", page: int = 1, per_page: int = 15) -> Dict[str, Any]:
        """Retrieves a list of countries based on search and pagination."""
        params = {
            "page": page,
            "per_page": per_page,
            "search": search,
            "sort_by": "created_at",
            "sort_order": "desc"
        }
        return await self.request("GET", "/countries", params=params)


    async def create_matter(self, matter_data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new matter record in MatterMiner Core."""
        payload = {
            "tenantId": self.tenant_id,
            **matter_data
        }
        logger.info(f"[MATTER-POST] Payload: {payload}")
        return await self.request("POST", "/matters", json_data=payload)

    async def lookup_clients(self, search: str = "") -> Dict[str, Any]:
        """Retrieves a list of clients based on search terms."""
        params = {"search": search, "tenantId": self.tenant_id}
        return await self.request("GET", "/client", params=params)

    async def lookup_practice_areas(self, search: str = "", is_search: int = 0) -> Dict[str, Any]:
        """Retrieves configured practice areas."""
        params = {
            "page": 1,
            "per_page": 15,
            "search": search,
            "sort_by": "created_at",
            "sort_order": "desc",
            "is_search": is_search,
            "tenantId": self.tenant_id
        }
        return await self.request("GET", "/practice-area", params=params)

    async def lookup_case_stages(self, search: str = "", is_search: int = 0) -> Dict[str, Any]:
        """Retrieves configured case stages list."""
        params = {
            "page": 1,
            "per_page": 15,
            "search": search,
            "sort_by": "created_at",
            "sort_order": "desc",
            "is_search": is_search,
            "tenantId": self.tenant_id
        }
        return await self.request("GET", "/case-stage", params=params)

    async def lookup_billing_info(self, search: str = "", is_search: int = 1) -> Dict[str, Any]:
        """Retrieves billing info and case stage ID via search."""
        params = {
            "search": search,
            "is_search": is_search,
            "tenantId": self.tenant_id
        }
        return await self.request("GET", "/billing-info", params=params)

    async def lookup_billing_types(self, search: str = "") -> Dict[str, Any]:
        """Retrieves configured billing types."""
        params = {"search": search, "tenantId": self.tenant_id}
        return await self.request("GET", "/billing-types", params=params)

    def _get_headers(self) -> Dict[str, str]:
        """Constructs headers with necessary context and static API key authorization."""
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": self.tenant_id,
            "Accept": "application/json",
            "Authorization": f"Bearer {settings.CORE_API_KEY}"  # Static API key auth (Phase 1: Auth Migration)
        }
        
        if self.user_email:
            headers["X-User-Email"] = self.user_email
            
        if self.correlation_id:
            headers["X-Correlation-ID"] = self.correlation_id
            
        return headers

    async def close(self):
        """Closes the underlying HTTP client."""
        await self.client.aclose()
