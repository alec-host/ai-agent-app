import httpx
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("legal-agentic-ai")

class MatterMinerCoreClient:
    """
    Client for interacting with the MatterMiner Core remote system.
    Authentication is handled by the backend; this client passes tenant context.
    """
    def __init__(self, base_url: str, tenant_id: str, user_email: Optional[str] = None, correlation_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/").replace("/app", "")
        self.tenant_id = tenant_id
        self.user_email = user_email
        self.correlation_id = correlation_id
        self.access_token = None
        self.user_profile = None
        
        # Internal async client
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            verify=False  # Assuming dev environment might have self-signed certs
        )

    def set_auth_token(self, token: str):
        self.access_token = token

    def is_authenticated(self) -> bool:
        return self.access_token is not None

    async def request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Reusable method for calling remote operations.
        Passes tenant information via headers.
        """
        url = f"{self.base_url}/app/{endpoint.lstrip('/')}"
        headers = self._get_headers()
        
        try:
            response = await self.client.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=headers
            )
            
            # --- REACTIVE AUTH DETECTION ---
            # If the service returns 404 with a "Not found" message, it signals session expiration.
            if response.status_code == 404:
                try:
                    data = response.json()
                    msg = str(data.get("message", "")).lower()
                    success = data.get("success")
                    # Catch "success": false/None and "Not found" (case-insensitive)
                    if msg == "not found":
                        logger.warning(f"[CORE-API] 404 Session Missing detected for {endpoint}. Triggering login workflow.")
                        return {"status": "auth_required", "code": 404, "message": "Authentication required."}
                except:
                    pass

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

    async def create_contact(self, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new contact record in the remote system."""
        payload = {
            "tenantId": self.tenant_id,
            **contact_data
        }
        return await self.request("POST", "/contact", json_data=payload)

    async def search_contact_by_email(self, email: str) -> Dict[str, Any]:
        """Searches for a contact by email and returns their contact_id."""
        params = {
            "search": email,
            "tenantId": self.tenant_id
        }
        return await self.request("GET", "/search-contact", params=params)

    async def create_contact(self, contact_data: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new contact record in MatterMiner Core."""
        payload = {
            "tenantId": self.tenant_id,
            **contact_data
        }
        logger.info(f"[CONTACT-POST] Payload: {payload}")
        return await self.request("POST", "/contact", json_data=payload)

    async def create_client(self, client_data: Dict[str, Any]) -> Dict[str, Any]:
        """Registers a new client record in MatterMiner Core."""
        payload = {
            "tenantId": self.tenant_id,
            **client_data
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

    async def has_valid_token(self, email: str) -> Dict[str, Any]:
        """
        Proactively checks if a user has a valid auth token on the remote system.
        Calls GET /hasValidToken?email=...&tenantId=...
        Returns the raw response; 404 triggers auth_required via self.request().
        """
        params = {"email": email, "tenantId": self.tenant_id}
        return await self.request("GET", "/hasValidToken", params=params)

    async def login(self, email: str, password: str) -> Dict[str, Any]:
        """
        Authenticates a user with the remote system.
        POST /login { email, password, tenantId }
        """
        payload = {
            "email": email,
            "password": password,
            "tenantId": self.tenant_id
        }
        resp = await self.request("POST", "/login", json_data=payload)
        if resp.get("status") == "success" or resp.get("success") is True:
            data = resp.get("data", {})
            if "token" in data:
                self.set_auth_token(data["token"])
            self.user_profile = data.get("user")
        return resp

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
        return await self.request("GET", "/clients", params=params)

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

    async def lookup_billing_info(self, search: str = "") -> Dict[str, Any]:
        """Retrieves billing info and case stage ID via search."""
        params = {
            "search": search,
            "is_search": 1,
            "tenantId": self.tenant_id
        }
        return await self.request("GET", "/billing-info", params=params)

    async def lookup_billing_types(self, search: str = "") -> Dict[str, Any]:
        """Retrieves configured billing types."""
        params = {"search": search, "tenantId": self.tenant_id}
        return await self.request("GET", "/billing-types", params=params)

    def _get_headers(self) -> Dict[str, str]:
        """Constructs headers with necessary context."""
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": self.tenant_id,
            "Accept": "application/json"
        }
        
        if self.user_email:
            headers["X-User-Email"] = self.user_email
            
        if self.correlation_id:
            headers["X-Correlation-ID"] = self.correlation_id
            
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            
        return headers

    async def close(self):
        """Closes the underlying HTTP client."""
        await self.client.aclose()
