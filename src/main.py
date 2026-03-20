import re
import os
import json
import uuid
import time
# FIX 1: Removed 'import datetime' module to prevent collision with 'from datetime import datetime'
import logging
import httpx
import dateparser

import sentry_sdk

from contextlib import asynccontextmanager
from typing import Optional, List
from fastapi import FastAPI, Header, HTTPException, Request, Depends, status, APIRouter
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse

# FIX 1 (cont): This is the preferred way to handle datetime in this file
from datetime import datetime, timedelta, timezone

from urllib.parse import quote

from src.tools import TOOLS
from src.prompts import get_legal_system_prompt
from src.agent_manager import execute_tool_call, get_rehydration_context
from src.utils import sanitize_history, retry_with_backoff, get_starter_chips

from src.config import settings
from src.logger import logger

from pydantic import BaseModel, ConfigDict
from openai import AsyncOpenAI

@asynccontextmanager
async def lifespan(app: FastAPI):
    # [STARTUP LOGIC]
    app.state.http_client = httpx.AsyncClient(
        base_url=settings.NODE_SERVICE_URL,
        headers={"User-Agent": "Legal-AI-Agent/1.0"},
        verify=False,
        timeout=httpx.Timeout(15.0)
    )
    
    app.state.ai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    print(f"--- {settings.APP_NAME} Started Successfully ---")
    
    yield  # --- The app runs here ---

    # [SHUTDOWN LOGIC]
    print(f"--- {settings.APP_NAME} Shutting Down ---")
    await app.state.http_client.aclose()
    print("Resources cleaned up. Goodbye.")

app = FastAPI(title=settings.APP_NAME,description=settings.APP_DESCRIPTION, lifespan=lifespan)

# --- 0. CORS & Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("legal-agentic-ai")

# --- 1. Health Check Endpoint ---
@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check(request: Request):
    health_status = {
        "service": settings.APP_NAME,
        "status": "online",
        "timestamp": datetime.now().isoformat()
    }
    
    # Try connecting to Node.js backend
    try:
        url = f"{settings.NODE_SERVICE_URL}/"
        response = await request.app.state.http_client.get(url)
        health_status["backend"] = "online" if response.status_code == 200 else "degraded"
    except Exception:
        health_status["backend"] = "unreachable"
    
    return health_status

    return health_status

# --- 2. Security & Multi-tenancy Guardrails ---
async def verify_tenant_access(
    x_tenant_id: str = Header(...), 
    x_user_timezone: str = Header("UTC"),
    user_role: str = Header("Associate"),
    x_user_email: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None)
):
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="X-Tenant-ID is required.")
    
    # Extract token if "Bearer " prefix exists
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:]
    
    if token:
        logger.info(f"[AUTH-GUARD] Header Token detected for tenant {x_tenant_id} (Length: {len(token)})")
    else:
        logger.warning(f"[AUTH-GUARD] No Header Token provided for tenant {x_tenant_id}")

    return {
        "tenant_id": x_tenant_id, 
        "role": user_role.lower(), 
        "timezone": x_user_timezone,
        "token": token,
        "user_email": x_user_email
    }

# ---0. Track token usage ---
async def update_tenant_wallet(tenant_id: str, usage_object, calendar_service):
    """
    Sends token usage to the wallet service using the service's authenticated headers.
    """
    if not usage_object or not tenant_id:
        return
        
    payload = {
        "tenantId": tenant_id,
        "prompt_tokens": usage_object.prompt_tokens,
        "completion_tokens": usage_object.completion_tokens,
        "total_tokens": usage_object.total_tokens,
        "timestamp": datetime.now(timezone.utc).isoformat()
    }
    
    try:
        # We use the existing calendar_service.request method 
        # because it already handles the Base URL and Authorization Headers.
        response = await calendar_service.request(
            "POST", 
            "/wallet/deplete", 
            json_data=payload
        )
        logger.info(f"[WALLET] Tokens deducted for {tenant_id}. Result: {response}")
    except Exception as e:
        logger.error(f"[WALLET] Background update failed for tenant {tenant_id}: {e}")

# --- 3. Node.js Backend API Client ---            
class CalendarServiceClient:
    """Async client to communicate with the existing Node.js Microservice."""

    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient, correlation_id: str, thread_id: str = None, access_token: str = None):
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        self.thread_id = thread_id or "default_session"
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
            query = f"/chat/session?tenantId={self.tenant_id}&threadId={self.thread_id}"
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

# --- 5. Request Models ---
class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="allow")
    role: str 
    content: Optional[str] = None
    tool_calls: Optional[list] = None 
    tool_call_id: Optional[str] = None
    name: Optional[str] = None

    
class ChatRequest(BaseModel):
    prompt: str
    history: Optional[List[ChatMessage]] = []
    debug: bool = False
    thread_id: Optional[str] = None

# --- 6. The Core Reasoning Endpoint ---
@app.post("/ai/chat")
async def handle_agent_query(req: ChatRequest, request: Request, auth: dict = Depends(verify_tenant_access)):
    user_prompt_raw = req.prompt.lower().strip()
    if user_prompt_raw in ["clear", "reset", "/clear"]:
        return {"response": "Conversation history cleared.", "history": []}

    tenant_id, user_role, corr_id = auth["tenant_id"], auth["role"], request.state.correlation_id
    user_email = auth.get("user_email")
    calendar_service = CalendarServiceClient(
        tenant_id, 
        request.app.state.http_client, 
        correlation_id=corr_id, 
        thread_id=req.thread_id,
        access_token=auth.get("token")
    )
    
    # Session Recovery: Restore JWT from history if present (CRITICAL: Fixes auth-healing on first turn)
    cleaned_history = [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in req.history]
    for msg in reversed(cleaned_history):
        content = msg.get("content") or ""
        if '"jwtToken":' in content:
            try:
                data = json.loads(content)
                if data.get("jwtToken"):
                    # NOTE: This replaces any token passed from verify_tenant_access
                    # with the one from history (which is the most recent JWT).
                    calendar_service.set_auth_token(data["jwtToken"], is_jwt=True)
                    break
            except: continue
    # --- Intent Gating (Proactive Auth Checks) ---
    calendar_keywords = [
        "schedule", "event", "meeting", "book", "appointment", "calendar",
        "set up a meeting", "setup a meeting", "arrange a meeting",
        "organize a meeting", "reschedule", "deposition"
    ]
    is_calendar_intent = any(kw in user_prompt_raw for kw in calendar_keywords)
    # Explicit system mentions to prevent cross-auth confusion
    is_explicit_google = any(kw in user_prompt_raw for kw in ["google", "personal", "external"])
    is_explicit_core = any(kw in user_prompt_raw for kw in ["matter", "firm", "internal", "matterminer", "deadline", "filing"])

    core_keywords = [
        "register", "onboard", "new client", "create client", "setup client",
        "contact", "country", "countries", "client", "investigate",
        "matter", "firm", "internal", "matterminer", "deadline", "filing"
    ]
    is_core_intent = any(kw in user_prompt_raw for kw in core_keywords)
    is_login_attempt = any(kw in user_prompt_raw for kw in ["login", "log in", "password"])
    
    # Only trigger Google Pre-flight if it is EXPLICITLY external
    if is_calendar_intent and is_explicit_google and not is_login_attempt:
        logger.info(f"[{tenant_id}] Calendar intent detected. Performing Auth Handshake.")

        # STEP 1: Sync JWT — fetches access token from Node.js and sets it in headers.
        # This is mandatory before any Node.js resource or grant-check call.
        token_status = await calendar_service._sync_access_token()
        if token_status["status"] == "auth_required":
            logger.warning(f"[{tenant_id}] Step 1: No session found. Returning auth_required.")
            return {
                "role": "assistant",
                "content": "Calendar Access Required",
                "message": "Google Calendar connection is required to schedule events.",
                "status": "auth_required",
                "auth_type": "google_calendar",
                "auth_url": token_status["auth_url"],
                "history": req.history
            }

        # STEP 2: Grant Check — with JWT now in headers, verify Google Calendar was actually granted.
        # Replaces the old live /events probe with a purpose-built endpoint.
        grant = await calendar_service.check_grant_token()
        if not grant["granted"]:
            logger.warning(f"[{tenant_id}] Step 2: hasGrantToken returned not granted. Re-auth required.")
            return {
                "role": "assistant",
                "content": "Calendar Access Required",
                "message": "Google Calendar connection is required to schedule events.",
                "status": "auth_required",
                "auth_type": "google_calendar",
                "auth_url": grant["auth_url"],
                "history": req.history
            }

    if is_core_intent and not is_login_attempt:
        logger.info(f"[CHAT] [{tenant_id}] Core intent detected. Checking token validity.")
        if not user_email:
            logger.warning(f"[CHAT] [{tenant_id}] No X-User-Email header. Surface login card.")
            return {
                "role": "assistant",
                "content": "Authentication Required",
                "message": "Please login to MatterMiner Core to proceed.",
                "status": "auth_required",
                "auth_type": "matterminer_core",
                "history": cleaned_history
            }
        from .remote_services.matterminer_core import MatterMinerCoreClient
        core_client = MatterMinerCoreClient(base_url=settings.NODE_REMOTE_SERVICE_URL, tenant_id=tenant_id)
        try:
            status = await core_client.has_valid_token(user_email)
            if status.get("status") == "auth_required" or status.get("code") == 404:
                 logger.warning(f"[CHAT] [{tenant_id}] Token invalid or missing for {user_email}. Surface login card.")
                 return {
                    "role": "assistant",
                    "content": "Authentication Required",
                    "message": "Your MatterMiner session has expired. Please login again.",
                    "status": "auth_required",
                    "auth_type": "matterminer_core",
                    "history": cleaned_history
                 }
            logger.info(f"[CHAT] [{tenant_id}] Core token valid for {user_email}. Proceeding.")
        finally:
            await core_client.close()

	# DROP-IN PRE-FLIGHT CHECK
    # wallet_check = await calendar_service.request("GET", f"/wallet/check-balance?tenantId={tenant_id}")
    '''
    if wallet_check.get("allowed") is False:
        error_type = wallet_check.get("error")
        if error_type == "insufficient_funds":
            return {
                "role": "assistant",
                "content": f"Your token wallet is low ({wallet_check.get('balance')} tokens). Please top up your balance to continue using the AI assistant."
            }
        elif error_type == "no_wallet":
             return {"role": "assistant", "content": "No wallet found for this account. Please contact support."}
    '''
    # --- 1. HISTORY CLEANUP (Already done during recovery) ---
    # cleaned_history = [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in req.history]
    
    # Session Recovery: Restore JWT from history if present
    # for msg in reversed(cleaned_history):
    #     content = msg.get("content") or ""
    #     if '"jwtToken":' in content:
    #         try:
    #             data = json.loads(content)
    #             if data.get("jwtToken"):
    #                 calendar_service.set_auth_token(data["jwtToken"])
    #                 break
    #         except: continue

    services = {"calendar": calendar_service}

    # --- 2. CONTEXT REHYDRATION (Source of Truth) ---
    # This fetches the latest saved state from the DB to prevent looping
    rehydration_data = await get_rehydration_context(tenant_id, services)
    
    user_tz = auth.get("timezone", "UTC")
    messages = [{"role": "system", "content": get_legal_system_prompt(tenant_id, user_role, user_tz, settings.SUPPORTED_TIMEZONES)}]
    
    if rehydration_data:
        messages[0]["content"] += f"\n\n{rehydration_data.get('injection', '')}"

    messages.extend(sanitize_history(cleaned_history))
    messages.append({"role": "user", "content": req.prompt})

    ai_client = request.app.state.ai_client
    last_action = "Waiting for input"

    # --- 3. AGENTIC REASONING LOOP ---
    for i in range(5):
        # Fetch current session state for precise injection
        db_session = await services['calendar'].get_client_session(tenant_id)
        
        # --- AGENT-LEVEL GATEKEEPER: Catch auth issues before AI even thinks ---
        # If we are in an active calendar workflow AND the user is still talking about calendar, verify auth.
        metadata = db_session.get("metadata", {})
        lifecycle = metadata.get("session_lifecycle", "active")
        active_workflow = metadata.get("active_workflow")
        # Treat 'cleared' workflow or 'completed' lifecycle as no active lock
        if active_workflow == "cleared" or lifecycle == "completed":
            active_workflow = None
        if i == 0 and active_workflow == "calendar" and is_calendar_intent:
             logger.info(f"[{tenant_id}] Turn {i}: Active calendar workflow + intent. Ensuring Auth Handshake.")
             # MANDATORY: Sync JWT before grant-check
             token_status = await services['calendar']._sync_access_token()
             if token_status["status"] == "auth_required":
                  logger.warning(f"[{tenant_id}] Turn {i}: Session expired/missing during loop. Returning auth_required.")
                  return {
                      "role": "assistant",
                      "content": "Calendar Access Required",
                      "message": "Google Calendar connection is required to schedule events.",
                      "status": "auth_required",
                      "auth_type": "google_calendar",
                      "auth_url": token_status["auth_url"],
                      "history": cleaned_history
                  }

             grant = await services['calendar'].check_grant_token()
             if not grant["granted"]:
                  logger.warning(f"[{tenant_id}] Turn {i}: Grant check failed. Surface auth card.")
                  return {
                      "role": "assistant",
                      "content": "Calendar Access Required",
                      "message": "Google Calendar connection is required to schedule events.",
                      "status": "auth_required",
                      "auth_type": "google_calendar",
                      "auth_url": grant["auth_url"],
                      "history": cleaned_history
                  }
        
        # Segment 1: Client Fields
        client_vault = {k: v for k, v in {
            "client_type": db_session.get("client_type"),
            "first_name": db_session.get("first_name"),
            "last_name": db_session.get("last_name"),
            "client_email": db_session.get("email")
        }.items() if v}
        
        # Segment 2: Event Draft (from metadata)
        metadata = db_session.get("metadata", {})
        dirty_event_draft = metadata.get("event_draft", {})
        # Only include if we have at least one truthy value besides the control flag
        event_draft = {k: v for k, v in dirty_event_draft.items() if v is not None}
        has_real_data = any(v for k, v in event_draft.items() if k != "optional_fields_requested")
        if not has_real_data: event_draft = {}
        
        # Build unified vault string for prompt
        vault_segments = []
        if client_vault: 
            clean_client = {k: v for k, v in client_vault.items() if v is not None}
            if clean_client: vault_segments.append(f"CLIENT: {clean_client}")
        if event_draft and active_workflow in ["google_calendar", "standard_event", "all_day_event"]: 
            vault_segments.append(f"EVENT_DRAFT: {event_draft}")
        
        # Segment 3: Contact Draft (from metadata)
        # Try both metadata key and top-level key (for redundancy)
        contact_draft = metadata.get("contact_draft", {})
        if not contact_draft:
             contact_draft = db_session.get("contact_draft", {})
        
        if contact_draft and active_workflow == "contact":
             vault_segments.append(f"CONTACT_DRAFT: {contact_draft}")

        vault_str = " | ".join(vault_segments) if vault_segments else "Empty"

        current_now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")

        state_injection = {
            "role": "system", 
            "content": (
                f"### SYSTEM STATE ###\n"
                f"NOW (UTC): {current_now_utc} | USER_TIMEZONE: {user_tz}\n"
                f"DATABASE VAULT (SAVED): {vault_str}\n"
                f"LAST_SYSTEM_ACTION: {last_action}\n"
                "--- RULES ---\n"
                "1. VAULT IS SUPREME: If a field is in VAULT, you ARE FORBIDDEN from asking for it. Move to the next task.\n"
                "2. PERSISTENCE: Data in VAULT is already in the database. Continue until the task is success.\n"
            )
        }
        
        # Combine messages with dynamic state injection
        loop_messages = [messages[0], state_injection] + messages[1:]

        logger.info(f"[AGENT-LOOP] Iteration {i} | Vault: {vault_str}")

        # --- RATE LIMIT PROTECTION: Backoff + Throttling ---
        if i > 0: await asyncio.sleep(0.5) 
        
        async def _call_openai():
            return await ai_client.chat.completions.create(
                model="gpt-4o", messages=loop_messages, tools=TOOLS, tool_choice="auto"
            )
        
        # Wrapped in backoff to handle 429s gracefully
        from src.utils import retry_with_backoff 
        response = await retry_with_backoff(retries=2, backoff_in_seconds=2)(_call_openai)()
        
        assistant_msg = response.choices[0].message
        
        # Track token usage asynchronously
        if hasattr(response, 'usage'):
            import asyncio 
            asyncio.create_task(update_tenant_wallet(tenant_id, response.usage, calendar_service))
        
        assistant_dict = assistant_msg.model_dump(exclude_none=True)
        messages.append(assistant_dict)

        if not assistant_msg.tool_calls:
            # --- FINAL RESPONSE DECORATION ---
            final_payload = {"response": assistant_msg.content, "history": messages[1:]}
            
            # If this is the start of a conversation and no data exists, suggest actions
            if len(cleaned_history) <= 1 and not (rehydration_data and rehydration_data.get("has_data")):
                final_payload["suggested_actions"] = get_starter_chips()
            
            return final_payload

        # --- 4. EXECUTE TOOL CALLS ---
        terminal_success_msg = None
        for tool_call in assistant_msg.tool_calls:
            # Dispatch directly to Agent Manager
            result = await execute_tool_call(tool_call, services, user_role, tenant_id, messages, user_email=user_email)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": json.dumps(result)})
            
            if isinstance(result, dict):
                # Detect Terminal Success (Gives us direct control over the final output)
                # CRITICAL: If auth is required, terminate loop immediately to show the card
                if result.get("status") == "auth_required":
                    last_action = "Google session expired. Presenting Auth link."
                    return {
                        "role": "assistant",
                        "content": result.get("message", "Authorization required."),
                        "status": "auth_required",
                        "auth_type": result.get("auth_type"),
                        "auth_url": result.get("auth_url"),
                        "history": messages[1:]
                    }
                
                # --- OPTIMIZATION: SHORT-CIRCUIT LOOP ON DATA COLLECTION ---
                # If a tool says "we are partially done, ask for X", don't go back to OpenAI.
                # Just return the tool's instruction directly to the user.
                if result.get("status") == "partial_success":
                    logger.info(f"[THROTTLE] Short-circuiting loop on partial_success for {tool_call.function.name}")
                    return {
                        "response": result.get("message"), 
                        "history": messages[1:],
                        "vault_data": await services['calendar'].get_client_session(tenant_id)
                    }

                if result.get("_exit_loop") and result.get("status") == "success":
                    terminal_success_msg = result.get("message")
                elif result.get("status") in ["success", "partial_success"] or result.get("_continue_chaining"):
                    last_action = f"Executed {tool_call.function.name}: {result.get('message', 'Processed')}"
                else:
                    last_action = f"Failed {tool_call.function.name}: {result.get('message', 'Unknown error')}"

        # If a tool marked itself as terminal success, we break the loop and return its message directly
        if terminal_success_msg and not assistant_msg.content:
             final_db = await services['calendar'].get_client_session(tenant_id)
             return {"response": terminal_success_msg, "history": messages[1:], "vault_data": final_db}
        elif terminal_success_msg:
             # If assistant already had words, append the success table to it
             final_resp = f"{assistant_msg.content}\n\n{terminal_success_msg}"
             final_db = await services['calendar'].get_client_session(tenant_id)
             return {"response": final_resp, "history": messages[1:], "vault_data": final_db}

    final_db = await services['calendar'].get_client_session(tenant_id)
    return {"response": messages[-1].get("content"), "history": messages[1:], "vault_data": final_db}

# --- 6.1. The Streaming Reasoning Endpoint ---
@app.post("/ai/chat/stream")
async def handle_streaming_query(req: ChatRequest, request: Request, auth: dict = Depends(verify_tenant_access)):
    tenant_id, user_role, corr_id = auth["tenant_id"], auth["role"], request.state.correlation_id
    user_email = auth.get("user_email")
    calendar_service = CalendarServiceClient(
        tenant_id, 
        request.app.state.http_client, 
        correlation_id=corr_id, 
        thread_id=req.thread_id,
        access_token=auth.get("token")
    )
    ai_client = request.app.state.ai_client
    user_tz = auth.get("timezone", "UTC")
    services = {"calendar": calendar_service}

    # Setup Context (Reused from /ai/chat - duplication preferred for stability as requested)
    cleaned_history = [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in req.history]
    for msg in reversed(cleaned_history):
        content = msg.get("content") or ""
        if '"jwtToken":' in content:
            try:
                data = json.loads(content)
                if data.get("jwtToken"):
                    calendar_service.set_auth_token(data["jwtToken"], is_jwt=True)
                    break
            except: continue

    # --- 0. PROGRAMMATIC INTENT GATE (PRE-LLM) ---
    user_prompt_raw = req.prompt.lower().strip()
    
    # 1. Define Boolean Flags
    calendar_keywords = [
        "schedule", "event", "meeting", "book", "appointment", "calendar",
        "set up a meeting", "setup a meeting", "arrange a meeting",
        "organize a meeting", "reschedule", "deposition"
    ]
    is_calendar_intent = any(kw in user_prompt_raw for kw in calendar_keywords)

    core_keywords = [
        "register", "onboard", "new client", "create client", "setup client",
        "contact", "country", "countries", "client", "investigate",
        "matter", "firm", "internal", "matterminer", "deadline", "filing"
    ]
    is_core_intent = any(kw in user_prompt_raw for kw in core_keywords)
    is_login_attempt = any(kw in user_prompt_raw for kw in ["login", "log in", "password"])

    is_explicit_google = any(kw in user_prompt_raw for kw in ["google", "personal", "external"])
    is_explicit_core = any(kw in user_prompt_raw for kw in ["matter", "firm", "internal", "matterminer", "deadline", "filing"])

    # 2. Pre-LLM Auth Guard
    if is_calendar_intent and is_explicit_google and not is_login_attempt:
        logger.info(f"[STREAM] [{tenant_id}] Calendar intent detected. Performing Auth Handshake.")

        # STEP 1: Sync JWT — fetches access token from Node.js and sets it in headers.
        # Mandatory before any Node.js resource or grant-check call.
        token_status = await calendar_service._sync_access_token()
        if token_status["status"] == "auth_required":
            logger.warning(f"[STREAM] [{tenant_id}] Step 1: No session found. Returning auth_required.")
            async def _no_session_gen():
                yield f"data: {json.dumps({'status': 'auth_required', 'auth_type': 'google_calendar', 'message': 'Google Calendar connection is required to schedule events.', 'auth_url': token_status['auth_url']})}\n\n"
            return StreamingResponse(_no_session_gen(), media_type="text/event-stream")

        # STEP 2: Grant Check — with JWT now in headers, verify Google Calendar was actually granted.
        grant = await calendar_service.check_grant_token()
        if not grant["granted"]:
            logger.warning(f"[STREAM] [{tenant_id}] Step 2: hasGrantToken returned not granted. Re-auth required.")
            async def _no_grant_gen():
                yield f"data: {json.dumps({'status': 'auth_required', 'auth_type': 'google_calendar', 'message': 'Google Calendar connection is required to schedule events.', 'auth_url': grant['auth_url']})}\n\n"
            return StreamingResponse(_no_grant_gen(), media_type="text/event-stream")

    if is_core_intent and not is_login_attempt:
        logger.info(f"[STREAM] [{tenant_id}] Core intent detected. Checking token validity.")
        if not user_email:
            logger.warning(f"[STREAM] [{tenant_id}] No X-User-Email header. Surface login card.")
            async def _no_email_gen():
                yield f"data: {json.dumps({'status': 'auth_required', 'auth_type': 'matterminer_core', 'message': 'Please login to MatterMiner Core to proceed.'})}\n\n"
            return StreamingResponse(_no_email_gen(), media_type="text/event-stream")
        from .remote_services.matterminer_core import MatterMinerCoreClient
        core_client = MatterMinerCoreClient(base_url=settings.NODE_REMOTE_SERVICE_URL, tenant_id=tenant_id)
        try:
            status = await core_client.has_valid_token(user_email)
            if status.get("status") == "auth_required" or status.get("code") == 404:
                 logger.warning(f"[STREAM] [{tenant_id}] Token invalid or missing for {user_email}. Surface login card.")
                 async def _no_core_gen():
                     yield f"data: {json.dumps({'status': 'auth_required', 'auth_type': 'matterminer_core', 'message': 'Your MatterMiner session has expired. Please login again.'})}\n\n"
                 return StreamingResponse(_no_core_gen(), media_type="text/event-stream")
            logger.info(f"[STREAM] [{tenant_id}] Core token valid for {user_email}. Proceeding.")
        finally:
            await core_client.close()

    # Standard check for CLEAR
    if user_prompt_raw in ["clear", "reset", "/clear"]:
        async def clear_gen():
            yield f"data: {json.dumps({'content': 'Conversation history cleared.', 'done': True, 'history': []})}\n\n"
        return StreamingResponse(clear_gen(), media_type="text/event-stream")

    rehydration_data = await get_rehydration_context(tenant_id, services)
    messages = [{"role": "system", "content": get_legal_system_prompt(tenant_id, user_role, user_tz, settings.SUPPORTED_TIMEZONES)}]
    if rehydration_data:
        messages[0]["content"] += f"\n\n{rehydration_data.get('injection', '')}"
    messages.extend(sanitize_history(cleaned_history))
    messages.append({"role": "user", "content": req.prompt})

    async def event_generator():
        nonlocal messages
        last_action = "Waiting for input"
        
        for i in range(5):
            # Fetch current session state — fault-tolerant: stream must not fail if Node.js is down
            try:
                db_session = await services['calendar'].get_client_session(tenant_id)
                    # --- AGENT-LEVEL GATEKEEPER: Catch auth issues before AI even thinks (STREAMING) ---
                # If we are in an active calendar workflow AND the user is still talking about calendar, verify auth.
                # This allows the user to "break out" to a different workflow (like contact) without being blocked by Google.
                metadata = db_session.get("metadata", {})
                lifecycle = metadata.get("session_lifecycle", "active")
                active_workflow = metadata.get("active_workflow")
                # Treat 'cleared' workflow or 'completed' lifecycle as no active lock
                if active_workflow == "cleared" or lifecycle == "completed":
                    active_workflow = None
                if i == 0 and active_workflow == "google_calendar" and is_calendar_intent:
                     logger.info(f"[STREAM] [{tenant_id}] Turn {i}: Active calendar workflow + intent. Ensuring Auth Handshake.")
                     # MANDATORY: Sync JWT before grant-check
                     token_status = await services['calendar']._sync_access_token()
                     if token_status["status"] == "auth_required":
                          logger.warning(f"[STREAM] [{tenant_id}] Turn {i}: Session expired during loop. Surface auth card.")
                          yield f"data: {json.dumps({'status': 'auth_required', 'auth_type': 'google_calendar', 'message': 'Google Calendar connection is required to schedule events.', 'auth_url': token_status['auth_url']})}\n\n"
                          return
 
                     grant = await services['calendar'].check_grant_token()
                     if not grant["granted"]:
                          logger.warning(f"[STREAM] [{tenant_id}] Turn {i}: Grant check failed. Surface auth card.")
                          yield f"data: {json.dumps({'status': 'auth_required', 'auth_type': 'google_calendar', 'message': 'Google Calendar connection is required to schedule events.', 'auth_url': grant['auth_url']})}\n\n"
                          return
            except Exception as e:
                logger.warning(f"[STREAM] Backend session fetch failed (non-fatal): {e}")
                db_session = {}
            client_vault = {k: v for k, v in {
                "client_type": db_session.get("client_type"),
                "first_name": db_session.get("first_name"), "last_name": db_session.get("last_name"), 
                "client_email": db_session.get("email")
            }.items() if v}
            # Segment 2: Event Draft (from metadata)
            # metadata is already defined above in the gatekeeper check
            event_draft = {k: v for k, v in metadata.get("event_draft", {}).items() if v is not None}
            if not any(v for k, v in event_draft.items() if k != "optional_fields_requested"): event_draft = {}

            vault_segments = []
            if client_vault: vault_segments.append(f"CLIENT: {client_vault}")
            if event_draft and active_workflow in ["google_calendar", "standard_event", "all_day_event"]: vault_segments.append(f"EVENT_DRAFT: {event_draft}")
            
            # Segment 3: Contact Draft (from metadata)
            # Try both metadata key and top-level key (for redundancy)
            contact_draft = metadata.get("contact_draft", {})
            if not contact_draft:
                 contact_draft = db_session.get("contact_draft", {})
            
            if contact_draft and active_workflow == "contact":
                 vault_segments.append(f"CONTACT_DRAFT: {contact_draft}")

            vault_str = " | ".join(vault_segments) if vault_segments else "Empty"
            logger.info(f"[STREAM] STARTING ROUND {i+1} | Vault: {vault_str}")

            state_injection = {
                "role": "system",
                "content": (
                    f"### SYSTEM STATE ###\nNOW (UTC): {datetime.now(timezone.utc).strftime('%I:%M %p UTC')} | USER_TIMEZONE: {user_tz}\n"
                    f"DATABASE VAULT (SAVED): {vault_str}\nLAST_SYSTEM_ACTION: {last_action}\n"
                    "--- RULES ---\n1. VAULT IS SUPREME. 2. PERSISTENCE: Data in VAULT is already in the database.\n"
                )
            }
            loop_messages = [messages[0], state_injection] + messages[1:]
            logger.info(f"[STREAM] Sending messages to OpenAI...")

            # --- RATE LIMIT PROTECTION: Backoff + Throttling ---
            if i > 0: await asyncio.sleep(0.5)
            
            async def _call_openai_stream():
                return await ai_client.chat.completions.create(
                    model="gpt-4o", messages=loop_messages, tools=TOOLS, tool_choice="auto", stream=True
                )
            
            try:
                from src.utils import retry_with_backoff
                stream = await retry_with_backoff(retries=2, backoff_in_seconds=2)(_call_openai_stream)()
            except Exception as e:
                logger.error(f"[STREAM] OpenAI error: {e}")
                yield f"data: {json.dumps({'content': f'OpenAI error: {str(e)}', 'done': True})}\n\n"
                return

            full_content = ""
            current_tool_calls = {} # index -> call

            logger.info(f"[STREAM] Iterating over chunks...")
            async for chunk in stream:
                if not chunk.choices: continue
                delta = chunk.choices[0].delta
                
                # Stream Content
                if delta.content:
                    full_content += delta.content
                    logger.debug(f"[STREAM] Content delta: {delta.content}")
                    yield f"data: {json.dumps({'content': delta.content})}\n\n"
                
                # Accumulate Tool Calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {"id": tc.id, "function": {"name": tc.function.name, "arguments": ""}}
                        if tc.function.arguments:
                            current_tool_calls[idx]["function"]["arguments"] += tc.function.arguments

            # Finalize this round of streaming
            if not current_tool_calls:
                logger.info(f"[STREAM] Response complete. Full Content Length: {len(full_content)}")
                messages.append({"role": "assistant", "content": full_content})
                # Terminal check: suggest actions
                final_payload = {"done": True, "history": messages[1:]}
                if len(cleaned_history) <= 1 and not (rehydration_data and rehydration_data.get("has_data")):
                    final_payload["suggested_actions"] = get_starter_chips()
                yield f"data: {json.dumps(final_payload)}\n\n"
                return

            logger.info(f"[STREAM] Found tool calls. Total: {len(current_tool_calls)}")
            # Execute Tools
            assistant_msg_dict = {"role": "assistant", "content": full_content or None, "tool_calls": []}
            for idx in sorted(current_tool_calls.keys()):
                call = current_tool_calls[idx]
                assistant_msg_dict["tool_calls"].append({
                    "id": call["id"], "type": "function", 
                    "function": {"name": call["function"]["name"], "arguments": call["function"]["arguments"]}
                })
            
            messages.append(assistant_msg_dict)
            terminal_success_msg = None

            for tool_call_data in assistant_msg_dict["tool_calls"]:
                # Mock a tool-call object for execute_tool_call
                class MockTool:
                    def __init__(self, d):
                        self.id = d["id"]
                        self.function = type('obj', (object,), d["function"])
                
                # yield f"data: {json.dumps({'content': f'\\n\\n*[AGENT]*: Executing `{tool_call_data['function']['name']}`...\\n'})}\n\n"
                # Standard progress signal
                tool_name = tool_call_data['function']['name']
                yield f"data: {json.dumps({'action': f'Executing {tool_name}...'})}\n\n"
                
                result = await execute_tool_call(MockTool(tool_call_data), services, user_role, tenant_id, messages, user_email=user_email)
                messages.append({"role": "tool", "tool_call_id": tool_call_data["id"], "name": tool_name, "content": json.dumps(result)})
                
                if isinstance(result, dict):
                    # Proactively yield auth link to stream if needed
                    if result.get("status") == "auth_required":
                        yield f"data: {json.dumps(result)}\n\n"
                        # TERMINATE THE ENTIRE GENERATOR IMMEDIATELY
                        logger.warning("[STREAM-AUTH] Killing generator due to auth_required.")
                        return 
                    
                    # --- OPTIMIZATION: SHORT-CIRCUIT STREAM LOOP ON DATA COLLECTION ---
                    if result.get("status") == "partial_success":
                        logger.info(f"[STREAM-THROTTLE] Short-circuiting loop on partial_success for {tool_name}")
                        yield f"data: {json.dumps({'content': result.get('message'), 'done': True, 'history': messages[1:]})}\n\n"
                        return
                    
                    if result.get("_exit_loop") and result.get("status") == "success":
                        terminal_success_msg = result.get("message")
                    last_action = f"Executed {tool_name}"

            if terminal_success_msg:
                final_text = f"\n\n{terminal_success_msg}"
                yield f"data: {json.dumps({'content': final_text})}\n\n"
                messages.append({"role": "assistant", "content": terminal_success_msg})
                yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"
                return

        # Fallback completion
        yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- 7. Utility Route for Google Sync ---
@app.post("/ai/sync")
async def trigger_sync(request: Request, auth: dict = Depends(verify_tenant_access)):
    calendar = CalendarServiceClient(
        auth["tenant_id"], 
        request.app.state.http_client, 
        getattr(request.state, "correlation_id", str(uuid.uuid4())),
        access_token=auth.get("token")
    )
    return await calendar.request("POST", "/events/sync-google")

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response

# --- Demo UI fallback ---
app.mount("/", StaticFiles(directory="demo-ui", html=True), name="ui")
