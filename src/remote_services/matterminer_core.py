import httpx
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger("legal-agentic-ai")

class MatterMinerCoreClient:
    """
    Client for interacting with the MatterMiner Core remote system.
    Handles authentication, token management, and provides a reusable request wrapper.
    """
    def __init__(self, base_url: str, tenant_id: str, correlation_id: Optional[str] = None):
        self.base_url = base_url.rstrip("/")
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        self.access_token: Optional[str] = None
        self.user_profile: Optional[Dict[str, Any]] = None
        
        # Internal async client
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            verify=False  # Assuming dev environment might have self-signed certs
        )

    def set_auth_token(self, token: str):
        """Sets the Bearer token for authenticated requests."""
        self.access_token = token

    async def login(self, email: str, password: str) -> Dict[str, Any]:
        """
        Authenticates a user and retrieves the access token and profile.
        
        Args:
            email: User's email address.
            password: User's password.
            
        Returns:
            The full API response as a dictionary.
        """
        url = f"{self.base_url}/login"
        payload = {
            "email": email,
            "password": password
        }
        
        try:
            headers = self._get_headers()
            response = await self.client.post(url, json=payload, headers=headers)
            
            data = response.json()
            if response.status_code == 200 and data.get("status") == "success":
                token_data = data.get("token", {})
                self.access_token = token_data.get("access_token")
                self.user_profile = data.get("data")
                logger.info(f"[CORE-AUTH] Login successful for: {email}")
            else:
                logger.warning(f"[CORE-AUTH] Login failed for {email}: {data.get('message', 'Unknown error')}")
                
            return data
            
        except Exception as e:
            logger.error(f"[CORE-AUTH] Exception during login: {e}")
            return {"status": "error", "message": str(e)}

    async def request(self, method: str, endpoint: str, json_data: Optional[Dict[str, Any]] = None, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Reusable and scalable method for calling various operations thereafter.
        Automatically injects Bearer token and correlation IDs.
        """
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        headers = self._get_headers(authenticated=True)
        
        try:
            response = await self.client.request(
                method=method,
                url=url,
                json=json_data,
                params=params,
                headers=headers
            )
            
            # Scalable result handling
            if response.status_code in [200, 201]:
                return response.json()
            elif response.status_code == 401:
                logger.warning(f"[CORE-API] 401 Unauthorized for {endpoint}. Token might be expired.")
                return {"status": "error", "code": 401, "message": "Unauthorized"}
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

    def _get_headers(self, authenticated: bool = False) -> Dict[str, str]:
        """Constructs headers with necessary context."""
        headers = {
            "Content-Type": "application/json",
            "X-Tenant-ID": self.tenant_id,
            "Accept": "application/json"
        }
        
        if self.correlation_id:
            headers["X-Correlation-ID"] = self.correlation_id
            
        if authenticated and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            
        return headers

    async def close(self):
        """Closes the underlying HTTP client."""
        await self.client.aclose()
