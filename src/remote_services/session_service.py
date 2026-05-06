# src/remote_services/session_service.py
import json
import httpx
import logging
from urllib.parse import quote
from typing import Optional, Dict, Any

from src.config import settings
from src.logger import logger

class SessionClient:
    """
    Dedicated client for managing session states and drafts (Vault) 
    via the MatterMiner Node.js backend. Decoupled from Calendar Service.
    """
    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient, correlation_id: str, thread_id: str = "default", access_token: str = None, user_email: str = None):
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        self.thread_id = thread_id
        self.access_token = access_token
        self.user_email = user_email
        
        self.base_url = settings.NODE_REMOTE_SERVICE_URL.rstrip("/")
        self.root_prefix = "/api" if "/api" in self.base_url.lower() else "/app"
        self.base_url = self.base_url.replace("/api", "").replace("/app", "").replace("/calendar", "").replace("/core", "")
        
        self.headers = {
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": correlation_id
        }
        if user_email:
            self.headers["X-User-Email"] = user_email
            
        if access_token:
            self.headers["Authorization"] = f"Bearer {access_token}"
            
        self.client = http_client
        self.timeout = httpx.Timeout(15.0)

    async def _do_request(self, method: str, path: str, json_data: dict = None):
        url = f"{self.base_url}{self.root_prefix}{path}"
        try:
            response = await self.client.request(
                method, url, json=json_data, headers=self.headers, timeout=self.timeout
            )
            
            if response.status_code >= 400:
                logger.error(f"[SESSION-API] Error {response.status_code}: {response.text}")
                return {"status": "error", "message": f"Server returned error {response.status_code}"}
                
            return response.json()
        except Exception as e:
            logger.error(f"[SESSION-API] Failure: {str(e)}", exc_info=True)
            return {"status": "error", "message": "The session service is unreachable."}

    async def get_client_session(self, tenant_id: str, user_email: str = None) -> Dict[str, Any]:
        """Fetches partial intake data from the Node.js chatsessions table."""
        try:
            effective_email = user_email or self.user_email
            query = f"/chat/session?tenantId={self.tenant_id}"
            if self.thread_id:
                query += f"&threadId={self.thread_id}"
            if effective_email:
                query += f"&userEmail={quote(effective_email)}"
                
            resp = await self._do_request("GET", query)
            
            if isinstance(resp, dict) and resp.get("status") == "error":
                return {}
                
            actual_data = resp
            if isinstance(resp, dict) and resp.get("status") == "success" and "data" in resp:
                actual_data = resp["data"]
                
            if isinstance(actual_data, dict):
                metadata = actual_data.get("metadata", {})
                if isinstance(metadata, str):
                    try: metadata = json.loads(metadata)
                    except: metadata = {}
                
                remote_token = metadata.get("remote_access_token")
                if remote_token and not self.access_token:
                    logger.info(f"[{tenant_id}] Harvested Login Token from Session Metadata.")
                    self.access_token = remote_token
                    if "Authorization" not in self.headers or not self.headers["Authorization"]:
                        self.headers["Authorization"] = f"Bearer {remote_token}"

            return actual_data if isinstance(actual_data, dict) else {}
        except Exception as e:
            logger.error(f"[SESSION] Error fetching session: {e}")
            return {}

    async def sync_client_session(self, payload: dict) -> bool:
        """Updates the Node.js chatsessions table with the latest drafts and state."""
        try:
            payload["threadId"] = self.thread_id
            if self.user_email:
                payload["userEmail"] = self.user_email
                
            response = await self._do_request("POST", "/chat/session", json_data=payload)
            
            if isinstance(response, dict) and response.get("status") == "error":
                return False

            return True
        except Exception as e:
            logger.error(f"[SESSION] Error syncing session: {e}")
            return False

    async def clear_client_session(self, tenant_id: str) -> bool:
        """Deletes the draft session once the intake is complete."""
        try:
            query = f"/chat/session?tenantId={tenant_id}"
            if self.thread_id:
                query += f"&threadId={self.thread_id}"
            if self.user_email:
                query += f"&userEmail={quote(self.user_email)}"
                
            response = await self._do_request("DELETE", query)
            
            if isinstance(response, dict) and response.get("status") == "error":
                return False
                
            logger.info(f"[SESSION-CLEAR] Session destroyed for tenant: {tenant_id}")
            return True
        except Exception as e:
            logger.error(f"[SESSION-CLEAR] Error calling delete: {e}")
            return False
