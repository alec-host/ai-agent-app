# src/remote_services/wallet_service.py

import httpx
from datetime import datetime, timezone
from src.config import settings
from src.logger import logger

class WalletClient:
    """
    Service client for managing the global token wallet and credit depletion.
    This governs usage for each client/tenant across all AI operations.
    """
    
    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient):
        self.tenant_id = tenant_id
        self.client = http_client
        self.base_url = settings.NODE_SERVICE_URL

    async def update_usage(self, usage_object, auth_headers: dict = None):
        """
        Sends token usage (prompt, completion, total) to the wallet service.
        """
        if not usage_object or not self.tenant_id:
            return
            
        payload = {
            "tenantId": self.tenant_id,
            "prompt_tokens": usage_object.prompt_tokens,
            "completion_tokens": usage_object.completion_tokens,
            "total_tokens": usage_object.total_tokens,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        try:
            # Note: We use the provided auth_headers or the client's default if available.
            # Usually, the dispatcher provides the service client that already has headers.
            url = f"{self.base_url}/wallet/deplete"
            response = await self.client.post(
                url, 
                json=payload, 
                headers=auth_headers,
                timeout=10.0
            )
            
            if response.status_code == 200:
                logger.info(f"[WALLET] Tokens deducted for {self.tenant_id}. Result: {response.json()}")
                return response.json()
            else:
                logger.error(f"[WALLET] Depletion failed ({response.status_code}): {response.text}")
                return None
                
        except Exception as e:
            logger.error(f"[WALLET] Background update failed for tenant {self.tenant_id}: {e}")
            return None

    async def check_balance(self, auth_headers: dict = None) -> dict:
        """
        Proactively checks if the tenant has sufficient funds to continue AI operations.
        """
        try:
            url = f"{self.base_url}/wallet/check-balance?tenantId={self.tenant_id}"
            response = await self.client.get(url, headers=auth_headers, timeout=10.0)
            
            if response.status_code == 200:
                return response.json()
            return {"allowed": True, "balance": "unknown"} # Fallback if service is down
            
        except Exception as e:
            logger.error(f"[WALLET] Balance check failed: {e}")
            return {"allowed": True, "error": str(e)}
