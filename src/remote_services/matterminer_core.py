import httpx
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("legal-agentic-ai")

class MatterMinerCoreClient:
    """
    Client for interacting with the MatterMiner Core remote system.
    Authentication is handled by the backend; this client passes tenant context.
    """
    def __init__(self, base_url: str, tenant_id: str, correlation_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        
        # Internal async client
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            verify=False  # Assuming dev environment might have self-signed certs
        )

    async def request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Reusable method for calling remote operations.
        Passes tenant information via headers.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
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

    async def create_contact(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Creates a new contact record in the remote system."""
        return await self.request("POST", "/contacts", json_data=payload)

    async def create_client(self, client_data: Dict[str, Any]) -> Dict[str, Any]:
        """Registers a new client record in MatterMiner Core."""
        payload = {
            "tenantId": self.tenant_id,
            **client_data
        }
        return await self.request("POST", "/clients", json_data=payload)

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

    def _get_headers(self) -> Dict[str, str]:
        """Constructs headers with necessary context."""
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": self.tenant_id,
            "Accept": "application/json"
        }
        
        if self.correlation_id:
            headers["X-Correlation-ID"] = self.correlation_id
            
        return headers

    async def close(self):
        """Closes the underlying HTTP client."""
        await self.client.aclose()
