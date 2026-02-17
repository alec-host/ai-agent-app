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

from src.utils import sanitize_history, retry_with_backoff

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
            if response.status_code in [400, 401] and "token" in response.text.lower():
                return {
                    "status": "ready_to_reauth",
                    "auth_required": True,
                    "auth_url": f"{settings.NODE_SERVICE_URL}/auth/google?tenant_id={self.tenant_id}"
                }
            return response.json()
        except Exception as e:
            logger.error(f"Backend Failure: {str(e)}")
            return {"error": "system_offline"}

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
    
    # --- 1. HISTORY REPAIR STEP (CRITICAL FIX FOR 400 ERROR) ---
    cleaned_history = [m.dict() for m in req.history]
    while cleaned_history and cleaned_history[-1].get("role") == "assistant" and cleaned_history[-1].get("tool_calls"):
        cleaned_history.pop()

    # --- PROACTIVE PARAMETER LOCK-IN ---
    locked_params = {"title": None, "startTime": None}
    for msg_dict in cleaned_history:
        content = str(msg_dict.get("content") or "")
        t_match = re.search(r"(?:title|subject|summary)(?:\s+is|:)?\s+['\"]?([^'\"\n]+)['\"]?", content, re.IGNORECASE)
        if t_match: locked_params["title"] = t_match.group(1)
        if msg_dict.get("tool_calls"):
            for tc in msg_dict["tool_calls"]:
                try:
                    tc_args_raw = tc.get("function", {}).get("arguments") if isinstance(tc, dict) else tc.function.arguments
                    p_args = json.loads(tc_args_raw)
                    if p_args.get("title"): locked_params["title"] = p_args["title"]
                    if p_args.get("startTime"): locked_params["startTime"] = p_args["startTime"]
                except: continue

    # Session Recovery
    for msg_dict in reversed(cleaned_history):
        content = msg_dict.get("content") or ""
        if '"jwtToken":' in content:
            try:
                calendar_service.set_auth_token(json.loads(content).get("jwtToken"))
                break
            except: continue

    services = {"calendar": calendar_service}
    messages = [{"role": "system", "content": get_legal_system_prompt(tenant_id, user_role)}]
    messages.extend(sanitize_history(cleaned_history))
    messages.append({"role": "user", "content": req.prompt})

    ai_client = request.app.state.ai_client
    last_action = "Waiting for input"

    # --- 4. AGENTIC LOOP ---
    user_tz = auth.get("timezone", "UTC")
    for i in range(5):
        current_now_utc = datetime.now(timezone.utc).strftime("%I:%M %p UTC")
        lock_str = ", ".join([f"{k}: {v}" for k, v in locked_params.items() if v])
        
        state_injection = {
            "role": "system", 
            "content": f"STATE: {last_action} | NOW (UTC): {current_now_utc} | LOCKED_PARAMETERS: {lock_str or 'None'}. DO NOT ask for items in LOCKED_PARAMETERS. | Note: If user says 'tomorrow', calculate based on {user_tz}."
        }
        
        if messages[-1]["role"] == "assistant" and messages[-1].get("tool_calls"):
            loop_messages = messages 
        else:
            loop_messages = [messages[0]] + [state_injection] + messages[1:]
        
        response = await ai_client.chat.completions.create(
            model="gpt-4o", messages=loop_messages, tools=TOOLS, tool_choice="auto"
        )
        
        assistant_msg = response.choices[0].message
        
        # PROACTIVE ARGUMENT INJECTION
        if assistant_msg.tool_calls:
            for tool_call in assistant_msg.tool_calls:
                if tool_call.function.name == "schedule_event":
                    try:
                        args = json.loads(tool_call.function.arguments)
                        if (not args.get("title") or args.get("title").lower() in ["unknown", "string", ""]) and locked_params.get("title"):
                            args["title"] = locked_params["title"]
                            tool_call.function.arguments = json.dumps(args)
                    except: pass

        assistant_dict = assistant_msg.model_dump(exclude_none=True)
        messages.append(assistant_dict)

        if not assistant_msg.tool_calls:
            return {"response": assistant_msg.content, "history": messages[1:]}

        # --- 5. EXECUTE TOOL CALLS ---
        for tool_call in assistant_msg.tool_calls:
            # --- CONFLICT CHECK INTERCEPTION ---
            if tool_call.function.name == "schedule_event":
                try:
                    args = json.loads(tool_call.function.arguments)
                    start = args.get("startTime")
                    duration = args.get("duration", 60)
                    end = calendar_service.calculate_end_time(start, duration)
                    
                    if start and end and await calendar_service.check_conflicts(start, end):
                        result = {
                            "status": "error",
                            "message": f"CONFLICT: There is already an event at {start}. Suggest a different time to the user."
                        }
                        messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": json.dumps(result)})
                        last_action = "Blocked by scheduling conflict"
                        continue # Skip to next tool call or loop iteration
                except Exception as e:
                    logger.error(f"Conflict check logic error: {e}")

            # Normal Tool Execution
            result = await execute_tool_call(tool_call, services, user_role, tenant_id, cleaned_history)
            messages.append({"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": json.dumps(result)})
            
            if tool_call.function.name == "schedule_event":
                try:
                    new_args = json.loads(tool_call.function.arguments)
                    if new_args.get("title"): locked_params["title"] = new_args["title"]
                except: pass

            if isinstance(result, dict) and (result.get("status") == "ready" or result.get("_continue_chaining")):
                last_action = f"System Ready after {tool_call.function.name}"

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