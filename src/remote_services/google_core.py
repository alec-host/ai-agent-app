# src/remote_services/google_core.py

import re
import json
import httpx
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
from typing import Optional, List, Dict, Any

from src.config import settings
from src.logger import logger
from src.utils import retry_with_backoff

class GoogleCalendarClient:
    """Async client to communicate with the existing Node.js Microservice for Google Calendar."""

    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient, correlation_id: str, thread_id: str = None, access_token: str = None):
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        self.thread_id = thread_id
        self.access_token = access_token # The token passed from frontend
        self.base_url = settings.NODE_SERVICE_URL
        self.headers = {
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": correlation_id
        }
        self._jwt_synced = False
        if access_token:
            self.set_auth_token(access_token) # This sets the login token, but it's not the calendar JWT
            
        self.client = http_client 
        self.timeout = httpx.Timeout(15.0)
        
    def set_auth_token(self, token: str, is_jwt: bool = False):
        self.headers["Authorization"] = f"Bearer {token}"
        if is_jwt:
            self._jwt_synced = True
        
    def is_authenticated(self) -> bool:
        return "Authorization" in self.headers

    async def _sync_access_token(self) -> dict:
        """
        Step 1 — JWT Provisioner.
        Calls GET /auth/accessToken?tenant_id=... and syncs the returned JWT
        into self.headers via set_auth_token().

        Returns:
          { "status": "ready" }                                     -> JWT synced, proceed to grant check
          { "status": "auth_required", "auth_url": "..." }          -> No session, must OAuth
        """
        try:
            url = f"{settings.NODE_SERVICE_URL}/auth/accessToken?tenant_id={self.tenant_id}"
            
            # Pass BOTH camelCase and snake_case for maximum compatibility
            if self.access_token:
                url += f"&accessToken={self.access_token}&access_token={self.access_token}"
                
            handshake_headers = self.headers.copy()
            if self.access_token:
                handshake_headers["Authorization"] = f"Bearer {self.access_token}"
            else:
                if "Authorization" in handshake_headers:
                    del handshake_headers["Authorization"]

            resp = await self.client.get(url, headers=handshake_headers, timeout=10)
            if resp.status_code != 200:
                logger.error(f"[ACCESS-TOKEN] Provisioner failed ({resp.status_code}) for {self.tenant_id}: {resp.text}")
                return {
                    "status": "auth_required",
                    "auth_type": "google_calendar",
                    "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}"
                }
            data = resp.json()
            if data.get("status") == "ready" and data.get("jwtToken"):
                self.set_auth_token(data["jwtToken"], is_jwt=True)
                logger.info(f"[ACCESS-TOKEN] JWT synced for tenant {self.tenant_id}")
                return {"status": "ready"}
            
            logger.warning(f"[ACCESS-TOKEN] Provisioner returned non-ready status: {data.get('status')}")
            return {
                "status": "auth_required",
                "auth_type": "google_calendar",
                "auth_url": data.get("auth_url") or f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}"
            }
        except Exception as e:
            logger.error(f"[ACCESS-TOKEN] Failed for {self.tenant_id}: {e}")
            return {
                "status": "auth_required",
                "auth_type": "google_calendar",
                "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}"
            }

    async def silent_refresh(self) -> bool:
        """
        Attempt to refresh the Google OAuth token silently using the Node.js backend.
        Requires active Bearer JWT in headers.
        """
        try:
            url = f"{settings.NODE_SERVICE_URL}/auth/googleRefreshToken"
            payload = {
                "tenant_id": self.tenant_id,
                "accessToken": self.access_token,
                "access_token": self.access_token
            }
            
            refresh_headers = self.headers.copy()
            if self.access_token:
                # Use the login token for auth-management endpoints
                refresh_headers["Authorization"] = f"Bearer {self.access_token}"

            resp = await self.client.post(url, json=payload, headers=refresh_headers, timeout=10)
            data = resp.json()
            if data.get("success"):
                logger.info(f"[SILENT-REFRESH] Successfully refreshed Google token for {self.tenant_id}")
                return True
            logger.warning(f"[SILENT-REFRESH] Refresh failed for {self.tenant_id}: {data.get('message')}")
            return False
        except Exception as e:
            logger.error(f"[SILENT-REFRESH] Request crashed: {e}")
            return False

    async def check_grant_token(self) -> dict:
        """
        Step 2 — Calendar Grant Validity Gate.
        Calls GET /auth/hasGrantToken?tenant_id=... WITH the JWT already in self.headers.
        If invalid, attempts a SILENT REFRESH before failing.

        Returns:
          { "granted": True }                                           -> Calendar access confirmed
          { "granted": False, "auth_url": "...", "reason": "..." }     -> Must re-auth
        """
        try:
            # Step 0: Ensure we have a JWT before even trying. 
            # This handles cases where the "Intent Gate" was skipped.
            if not getattr(self, "_jwt_synced", False):
                logger.info(f"[GRANT-CHECK] JWT not synced yet for {self.tenant_id}. Syncing first...")
                await self._sync_access_token()

            url = f"{settings.NODE_SERVICE_URL}/auth/hasGrantToken?tenant_id={self.tenant_id}"
            # Try with current JWT headers first
            resp = await self.client.get(url, headers=self.headers, timeout=10)
            
            # If 401/403, immediately try to REFRESH the JWT via Provisioner
            if resp.status_code in [401, 403]:
                logger.warning(f"[GRANT-CHECK] 401 on hasGrantToken for {self.tenant_id}. Attempting JWT Sync refresh...")
                sync_resp = await self._sync_access_token()
                
                if sync_resp.get("status") == "ready":
                    logger.info(f"[GRANT-CHECK] JWT Refreshed. Retrying hasGrantToken...")
                    resp = await self.client.get(url, headers=self.headers, timeout=10)
                elif self.access_token:
                    logger.info(f"[GRANT-CHECK] JWT Sync failed. Retrying with Login Token fallback header...")
                    alt_headers = self.headers.copy()
                    alt_headers["Authorization"] = f"Bearer {self.access_token}"
                    resp = await self.client.get(url, headers=alt_headers, timeout=10)

            data = resp.json()

            # 1. SUCCESS: Grant is valid
            if data.get("success") and data.get("valid"):
                return {"granted": True}

            # 2. RECOVERY: Try Silent Refresh
            logger.info(f"[GRANT-CHECK] Grant invalid for {self.tenant_id}. Attempting recovery...")
            if await self.silent_refresh():
                # Re-verify after successful refresh
                resp = await self.client.get(url, headers=self.headers, timeout=10)
                data = resp.json()
                if data.get("success") and data.get("valid"):
                    logger.info(f"[GRANT-CHECK] Recovery successful for {self.tenant_id}")
                    return {"granted": True}

            # 3. FAILURE: Must re-authorize
            return {
                "granted": False,
                "auth_type": "google_calendar",
                "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}",
                "reason": data.get("message", "Google Calendar access required.")
            }
        except Exception as e:
            logger.error(f"[GRANT-CHECK] check_grant_token failed for {self.tenant_id}: {e}")
            return {
                "granted": False,
                "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}",
                "reason": "Auth service unreachable."
            }

    async def get_workflow_protocol(self, query: str, tenant_id: str) -> str:
        """
        Retrieves relevant legal protocols and workflow steps from the 
        Node.js RAG service to provide context for the AI Agent.
        """
        logger.info(f"[RAG-TRACE] Requesting protocol for: '{query}' (Tenant: {tenant_id})")
        try:
            params = {
                "query": query,
                "tenantId": tenant_id
            }
            # Calling the Node.js endpoint we created above
            response = await self.client.get(
                "/rag/lookup", 
                params=params, 
                headers=self.headers,
                timeout=10
            )

            logger.info(f"[RAG-TRACE] Node Response Status: {response.status_code}")

            if response.status_code != 200:
                logger.error(f"[RAG-TRACE] Node Error Body: {response.text}")
                return "Protocol service unavailable. Continue using VAULT_DATA."
            
            response.raise_for_status()
            
            data = response.json()

            context = data.get("context", "No protocol found.")

            logger.info(f"[RAG-TRACE] Context Retrieved: {context[:100]}...")

            return context

        except Exception as e:
            logger.error(f"[RAG-TRACE] CRITICAL FAILURE: {str(e)}", exc_info=True)
            return "Knowledge base error. DO NOT RESTART INTAKE. Rely on VAULT_DATA."

    def _get_local_offset(self) -> str:
        """Helper to get the server's local UTC offset (e.g. +03:00)"""
        now = datetime.now().astimezone()
        offset = now.strftime("%z")
        return f"{offset[:3]}:{offset[3:]}"

    @retry_with_backoff(retries=3, backoff_in_seconds=1)
    async def _do_request(self, method: str, url: str, json_data: dict):
        return await self.client.request(
            method, url, json=json_data, headers=self.headers, timeout=self.timeout
        )

    def ensure_timezone_offset(self, iso_string: str) -> str:
            if not iso_string or re.search(r"Z$|[+-]\d{2}:?\d{2}$", iso_string):
                return iso_string
            return f"{iso_string}{self._get_local_offset()}"

    def calculate_end_time(self, start_iso: str, duration_min: int, **kwargs) -> str:
        try:
            safe_start = self.ensure_timezone_offset(start_iso).replace('Z', '+00:00')
            start_dt = datetime.fromisoformat(safe_start)
            end_dt = start_dt + timedelta(minutes=duration_min)
            return end_dt.isoformat()
        except Exception as e:
            logger.error(f"Time calculation error: {e}")
            return None            
            
    async def request(self, method: str, path: str, json_data: dict = None, _retry_on_auth: bool = True):
        url = f"{settings.NODE_SERVICE_URL}{path}"
        if json_data and isinstance(json_data, dict):
            for field in ["startTime", "endTime"]:
                val = json_data.get(field)
                if val and isinstance(val, str) and not re.search(r"Z$|[+-]\d{2}:?\d{2}$", val):
                    json_data[field] = f"{val}{self._get_local_offset()}"

        try:
            response = await self._do_request(method, url, json_data)
            
            # --- SILENT AUTH HEALING / TOKEN CHECK ---
            # Broaden: Treat 401/403 and some 400s as points where we should check/refresh session
            resp_body = response.text
            is_potential_auth_issue = response.status_code in [401, 403] or (response.status_code == 400 and ("token" in resp_body.lower() or "unauthorized" in resp_body.lower() or "google" in resp_body.lower()))
            
            if is_potential_auth_issue and _retry_on_auth:
                logger.info(f"[AUTH-HEAL] Potential auth issue ({response.status_code}) for {path}. Verifying session...")
                
                # Re-sync JWT using the hardened provisioner logic
                auth_data = await self._sync_access_token()
                
                if auth_data.get("status") == "ready":
                    logger.info(f"[AUTH-HEAL] Session successfully synced for {self.tenant_id}. Retrying {path}...")
                    return await self.request(method, path, json_data, _retry_on_auth=False)
                
                # If sync confirms auth is required
                if auth_data.get("status") == "auth_required":
                     logger.warning(f"[AUTH-HEAL] Internal status confirms auth required for {self.tenant_id}.")
                     return {
                        "status": "auth_required",
                        "auth_url": auth_data.get("auth_url") or f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}",
                        "message": "Calendar Access Required",
                        "code": 401
                     }

            # --- AUTH RECOVERY INTERCEPTION (CRITICAL GATE) ---
            if is_potential_auth_issue:
                logger.warning(f"[AUTH-GUARD] Authentication block for {path}. Redirecting to OAuth.")
                return {
                    "status": "auth_required",
                    "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}",
                    "message": "Calendar Access Required",
                    "code": response.status_code
                }

            if response.status_code >= 400:
                logger.error(f"Backend API Error {response.status_code}: {resp_body}")
                return {"status": "error", "message": f"Server returned error {response.status_code}", "details": resp_body}

            return response.json()
        except Exception as e:
            logger.error(f"Backend Failure: {str(e)}", exc_info=True)
            return {"status": "error", "message": "The calendar service is currently offline or unreachable."}

    async def check_conflicts(self, start_iso: str, end_iso: str) -> bool:
        """
        Calls the Node.js check-conflicts endpoint with URL encoding.
        """
        # 1. Clean up the timestamps
        start_iso = self.ensure_timezone_offset(start_iso)
        end_iso = self.ensure_timezone_offset(end_iso)
        
        # 2. URL Encode the timestamps (converts '+' to '%2B')
        safe_start = quote(start_iso)
        safe_end = quote(end_iso)
        
        query_path = f"/events/check-conflicts?startTime={safe_start}&endTime={safe_end}"
        
        try:
            response = await self.request("GET", query_path)
            
            # Log the result so you can see it in your terminal
            logger.info(f"[CONFLICT CHECK] Result: {response}")
            
            if isinstance(response, dict) and response.get("hasConflict") is True:
                return True
            return False
        except Exception as e:
            logger.error(f"[CONFLICT CHECK] Request failed: {e}")
            return False
            
    async def get_client_session(self, tenant_id: str):
        """Fetches partial intake data from the Node.js chatsessions table."""
        try:
            # Use self.request to ensure Auth Headers and Auth-Healing are applied
            query = f"/chat/session?tenantId={self.tenant_id}"
            if self.thread_id:
                query += f"&threadId={self.thread_id}"
            resp = await self.request("GET", query)
            
            # HARDEN: If request returns an error object, return {} so agents start fresh
            if isinstance(resp, dict) and resp.get("status") == "error":
                return {}
            
            # AUTOMATIC UNWRAPPING: Handle Node.js standard response envelopes
            actual_data = resp
            if isinstance(resp, dict) and resp.get("status") == "success" and "data" in resp:
                logger.info(f"[DB-SESSION] Unwrapped session data for {tenant_id}")
                actual_data = resp["data"]
            
            # TOKEN RECOVERY: If we don't have a token in memory but it's in the DB, harvest it.
            if isinstance(actual_data, dict):
                metadata = actual_data.get("metadata", {})
                if isinstance(metadata, str):
                    try: metadata = json.loads(metadata)
                    except: metadata = {}
                
                remote_token = metadata.get("remote_access_token")
                if remote_token and not self.access_token:
                    logger.info(f"[{tenant_id}] Harvested Login Token from Session Metadata.")
                    self.access_token = remote_token
                    # Only override header if it's empty to avoid stomping valid JWTs
                    if "Authorization" not in self.headers or not self.headers["Authorization"]:
                        self.set_auth_token(remote_token)

            return actual_data if isinstance(actual_data, dict) else {}
        except Exception as e:
            logger.error(f"Error fetching session: {e}")
            return {}

    async def sync_client_session(self, payload: dict):
        """Updates the Node.js chatsessions table with latest client_number,client_type,first_name,last_name,email, and history."""
        try:
            # ENSURE THREAD ID: Guarantee the payload has the threadId to prevent Node.js 500
            payload["threadId"] = self.thread_id
            
            # Use self.request to ensure Auth Headers and Auth-Healing are applied
            response = await self.request("POST", "/chat/session", json_data=payload)
            
            if isinstance(response, dict) and response.get("status") == "error":
                return False

            return True
        except Exception as e:
            logger.error(f"Error syncing session: {e}")
            return False

    async def clear_client_session(self, tenant_id: str):
        """Deletes the draft session once the intake is complete."""
        try:
            params = {"tenantId": tenant_id}
            if getattr(self, 'thread_id', None):
                params["threadId"] = self.thread_id
            
            query = f"/chat/session?tenantId={params['tenantId']}"
            if "threadId" in params:
                query += f"&threadId={params['threadId']}"
                
            response = await self.request("DELETE", query)
            
            if isinstance(response, dict) and response.get("status") == "error":
                return False
                
            logger.info(f"[DB-CLEAR] Session destroyed for tenant: {tenant_id}")
            return True
        except Exception as e:
            logger.error(f"[DB-CLEAR] Error calling delete: {e}")
            return False
