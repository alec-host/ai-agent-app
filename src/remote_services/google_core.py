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

    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient, correlation_id: str, thread_id: str = None, access_token: str = None, user_email: str = None):
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        self.thread_id = thread_id or "default"
        self.access_token = access_token # The token passed from frontend
        self.user_email = user_email
        # Flexible Base URL: Honor the .env path but ensure clean joining (Architectural Guard)
        self.base_url = settings.NODE_REMOTE_SERVICE_URL.rstrip("/")
        # Determine the root prefix (/app or /api) from settings, fallback to /app
        self.root_prefix = "/api" if "/api" in self.base_url.lower() else "/app"
        # Strip the prefix from base_url to prevent double-prefixing in request()
        self.base_url = self.base_url.replace("/api", "").replace("/app", "").replace("/calendar", "").replace("/core", "")
        self.headers = {
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": correlation_id
        }
        if user_email:
            self.headers["X-User-Email"] = user_email
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
                "/app/rag/lookup", 
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
        url = f"{self.base_url}{self.root_prefix}{path}"
        if json_data and isinstance(json_data, dict):
            for field in ["startTime", "endTime"]:
                val = json_data.get(field)
                if val and isinstance(val, str) and not re.search(r"Z$|[+-]\d{2}:?\d{2}$", val):
                    json_data[field] = f"{val}{self._get_local_offset()}"

        try:
            response = await self._do_request(method, url, json_data)
            
            # --- AUTH RECOVERY INTERCEPTION (CRITICAL GATE) ---
            # Broaden: Treat 401/403 and some 400s as points where we should check/refresh session
            resp_body = response.text
            is_potential_auth_issue = response.status_code in [401, 403] or (response.status_code == 400 and ("token" in resp_body.lower() or "unauthorized" in resp_body.lower() or "google" in resp_body.lower()))
            

            # --- AUTH RECOVERY INTERCEPTION (CRITICAL GATE) ---
            if is_potential_auth_issue:
                logger.warning(f"[AUTH-GUARD] Authentication block for {path}. Redirecting to OAuth.")
                return {
                    "status": "error",
                    "message": "Calendar authentication failed. Service unavailable.",
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
            
    async def get_client_session(self, tenant_id: str, user_email: str = None):
        """Fetches partial intake data from the Node.js chatsessions table."""
        try:
            # Identity Resolution: Prioritize method argument, fallback to instance state
            effective_email = user_email or self.user_email
            
            # Use self.request to ensure Auth Headers and Auth-Healing are applied
            query = f"/chat/session?tenantId={self.tenant_id}"
            if self.thread_id:
                query += f"&threadId={self.thread_id}"
            if effective_email:
                query += f"&userEmail={quote(effective_email)}"
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
            # Identity Resolution
            effective_email = self.user_email
            
            # ENSURE THREAD ID: Guarantee the payload has the threadId to prevent Node.js 500
            payload["threadId"] = self.thread_id
            if effective_email:
                payload["userEmail"] = effective_email
            
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
            query = f"/chat/session?tenantId={tenant_id}"
            
            thread_id = getattr(self, 'thread_id', None)
            if thread_id:
                query += f"&threadId={thread_id}"

            if self.user_email:
                query += f"&userEmail={quote(self.user_email)}"
                
            response = await self.request("DELETE", query)
            
            if isinstance(response, dict) and response.get("status") == "error":
                return False
                
            logger.info(f"[DB-CLEAR] Session destroyed for tenant: {tenant_id}")
            return True
        except Exception as e:
            logger.error(f"[DB-CLEAR] Error calling delete: {e}")
            return False
