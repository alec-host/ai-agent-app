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

# FIX 1 (cont): This is the preferred way to handle datetime in this file
from datetime import datetime, timedelta, timezone

from urllib.parse import quote

from src.tools import TOOLS
from src.prompts import get_legal_system_prompt
from src.agent_manager import execute_tool_call

from src.utils import sanitize_history, retry_with_backoff, get_rehydration_context

from src.config import settings
from src.logger import logger

from pydantic import BaseModel
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("legal-agentic-ai")

# --- 1. Health Check Endpoint ---
@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    health_status = {
        "service": settings.APP_NAME,
        "status": "online",
        "dependencies": {
            "node_backend": "unknown",
            "openai_api": "connected" 
        }
    }
    try:
        response = await app.state.http_client.get("/", timeout=5.0)
        if response.status_code < 500:
            health_status["dependencies"]["node_backend"] = "reachable"
        else:
            health_status["dependencies"]["node_backend"] = "error_response"
    except Exception as e:
        logger.error(f"Health check failed to reach Node: {str(e)}") 
        health_status["status"] = "degraded"
        health_status["dependencies"]["node_backend"] = f"unreachable: {str(e)}"

    return health_status

# --- 2. Security & Multi-tenancy Guardrails ---
async def verify_tenant_access(
    x_tenant_id: str = Header(...), 
    x_user_timezone: str = Header("UTC"),
    user_role: str = Header("Associate")
):
    if not x_tenant_id:
        raise HTTPException(status_code=401, detail="X-Tenant-ID is required.")
    return {"tenant_id": x_tenant_id, "role": user_role.lower(), "timezone": x_user_timezone}

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
    def __init__(self, tenant_id: str, http_client: httpx.AsyncClient, correlation_id: str):
        self.tenant_id = tenant_id
        self.correlation_id = correlation_id
        self.headers = {
            "X-Tenant-ID": tenant_id,
            "X-Correlation-ID": correlation_id
        }
        self.client = http_client 
        self.timeout = httpx.Timeout(15.0)
        
    def set_auth_token(self, token: str):
        self.headers["Authorization"] = f"Bearer {token}"
        
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
            
    async def request(self, method: str, path: str, json_data: dict = None):
        url = f"{settings.NODE_SERVICE_URL}{path}"
        if json_data and isinstance(json_data, dict):
            for field in ["startTime", "endTime"]:
                val = json_data.get(field)
                if val and isinstance(val, str) and not re.search(r"Z$|[+-]\d{2}:?\d{2}$", val):
                    json_data[field] = f"{val}{self._get_local_offset()}"

        try:
            response = await self._do_request(method, url, json_data)
            
            # --- AUTH RECOVERY INTERCEPTION ---
            # 401 (Expired) or 403 (No Consent) from the Node.js backend
            if response.status_code in [401, 403]:
                logger.warning(f"[AUTH-GUARD] {response.status_code} received for {path}. Redirecting to OAuth.")
                return {
                    "status": "auth_required",
                    "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}",
                    "message": "Your Google session has expired or requires consent. Please re-authorize.",
                    "code": response.status_code
                }

            if response.status_code >= 400:
                logger.error(f"Backend API Error {response.status_code}: {response.text}")
                return {"status": "error", "message": f"Server returned error {response.status_code}", "details": response.text}

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

    async def save_new_client(self, client_data: dict, tenant_id: str):
        """
        Finalizes the client record by moving it from a 'chat session' 
        to the permanent 'clients' table.
        """
        payload = {
            "tenantId": tenant_id,
            "client_number": client_data.get("client_number"),
            "client_type": client_data.get("client_type"),
            "first_name": client_data.get("first_name"),
            "last_name": client_data.get("last_name"),
            "email": client_data.get("email")
        }

        # This should hit your Node.js permanent storage endpoint
        # return await self.request("POST", "/calendar/api/web", payload)
        return await self.client.post("/api/web", json=payload)
    
    async def get_client_session(self, tenant_id: str):
        """Fetches partial intake data from the Node.js chatsessions table."""
        try:
            response = await self.client.get("/chat/session", params={"tenantId": tenant_id})
            return response.json() if response.status_code == 200 else {}
        except Exception as e:
            logger.error(f"Error fetching session: {e}")
            return {}

    async def sync_client_session(self, payload: dict):
        """Updates the Node.js chatsessions table with latest client_number,client_type,first_name,last_name,email, and history."""
        try:
            response = await self.client.post("/chat/session", json=payload)

            logger.info(f"[DB-SYNC] Status: {response.status_code} | Payload: {payload}")

            return response.status_code == 200
        except Exception as e:
            logger.error(f"Error syncing session: {e}")
            return False

    async def clear_client_session(self, tenant_id: str):
        """Deletes the draft session once the intake is complete."""
        try:
            response = await self.client.delete("/chat/session", params={"tenantId": tenant_id})
            if response.status_code == 200:
                logger.info(f"[DB-CLEAR] Session destroyed for tenant: {tenant_id}")
                return True
            else:
                logger.warning(f"[DB-CLEAR] Failed to clear session. Status: {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"[DB-CLEAR] Error calling delete: {e}")
            return False

# --- 5. Request Models ---
class ChatMessage(BaseModel):
    role: str 
    content: Optional[str] = None
    tool_calls: Optional[list] = None 
    
class ChatRequest(BaseModel):
    prompt: str
    history: Optional[List[ChatMessage]] = []
    debug: bool = False

# --- 6. The Core Reasoning Endpoint ---
@app.post("/ai/chat")
async def handle_agent_query(req: ChatRequest, request: Request, auth: dict = Depends(verify_tenant_access)):
    user_prompt_raw = req.prompt.lower().strip()
    if user_prompt_raw in ["clear", "reset", "/clear"]:
        return {"response": "Conversation history cleared.", "history": []}

    tenant_id, user_role, corr_id = auth["tenant_id"], auth["role"], request.state.correlation_id
    calendar_service = CalendarServiceClient(tenant_id, request.app.state.http_client, correlation_id=corr_id)
    
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
    # --- 1. HISTORY CLEANUP ---
    cleaned_history = [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in req.history]
    
    # Session Recovery: Restore JWT from history if present
    for msg in reversed(cleaned_history):
        content = msg.get("content") or ""
        if '"jwtToken":' in content:
            try:
                data = json.loads(content)
                if data.get("jwtToken"):
                    calendar_service.set_auth_token(data["jwtToken"])
                    break
            except: continue

    services = {"calendar": calendar_service}

    # --- 2. CONTEXT REHYDRATION (Source of Truth) ---
    # This fetches the latest saved state from the DB to prevent looping
    rehydration_block = await get_rehydration_context(tenant_id, services)
    
    messages = [{"role": "system", "content": get_legal_system_prompt(tenant_id, user_role)}]
    if rehydration_block:
        messages[0]["content"] += f"\n\n{rehydration_block}"

    messages.extend(sanitize_history(cleaned_history))
    messages.append({"role": "user", "content": req.prompt})

    ai_client = request.app.state.ai_client
    last_action = "Waiting for input"
    user_tz = auth.get("timezone", "UTC")

    # --- 3. AGENTIC REASONING LOOP ---
    for i in range(5):
        # Fetch current session state for precise injection
        db_session = await services['calendar'].request("GET", f"/chat/session?tenantId={tenant_id}")
        
        # Segment 1: Client Fields
        client_vault = {k: v for k, v in {
            "client_number": db_session.get("client_number"),
            "client_type": db_session.get("client_type"),
            "first_name": db_session.get("first_name"),
            "last_name": db_session.get("last_name"),
            "email": db_session.get("email")
        }.items() if v}
        
        # Segment 2: Event Draft (from metadata)
        metadata = db_session.get("metadata", {})
        event_draft = metadata.get("event_draft", {})
        
        # Build unified vault string for prompt
        vault_segments = []
        if client_vault: vault_segments.append(f"CLIENT: {client_vault}")
        if event_draft: vault_segments.append(f"EVENT_DRAFT: {event_draft}")
        vault_str = " | ".join(vault_segments) if vault_segments else "Empty"

        current_now_utc = datetime.now(timezone.utc).strftime("%I:%M %p UTC")

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

        response = await ai_client.chat.completions.create(
            model="gpt-4o", messages=loop_messages, tools=TOOLS, tool_choice="auto"
        )
        
        assistant_msg = response.choices[0].message
        
        # Track token usage asynchronously
        if hasattr(response, 'usage'):
            import asyncio 
            asyncio.create_task(update_tenant_wallet(tenant_id, response.usage, calendar_service))
        
        assistant_dict = assistant_msg.model_dump(exclude_none=True)
        messages.append(assistant_dict)

        if not assistant_msg.tool_calls:
            return {"response": assistant_msg.content, "history": messages[1:]}

        # --- 4. EXECUTE TOOL CALLS ---
        for tool_call in assistant_msg.tool_calls:
            # Dispatch directly to Agent Manager
            result = await execute_tool_call(tool_call, services, user_role, tenant_id, messages)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": json.dumps(result)})
            
            if isinstance(result, dict):
                # SUCCESS / PROGRESS
                if result.get("status") in ["success", "partial_success"] or result.get("_continue_chaining"):
                    last_action = f"Executed {tool_call.function.name}: {result.get('message', 'Processed')}"
                
                # AUTH EXPIRED
                elif result.get("status") == "auth_required":
                    last_action = "Google session expired. Presenting Auth link."
                
                # ERROR
                else:
                    last_action = f"Failed {tool_call.function.name}: {result.get('message', 'Unknown error')}"

    return {"response": messages[-1].get("content"), "history": messages[1:]}

# --- 7. Utility Route for Google Sync ---
@app.post("/ai/sync")
async def trigger_sync(request: Request, auth: dict = Depends(verify_tenant_access)):
    calendar = CalendarServiceClient(auth["tenant_id"], request.app.state.http_client, getattr(request.state, "correlation_id", str(uuid.uuid4())))
    return await calendar.request("POST", "/events/sync-google")

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response
