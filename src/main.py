import re
import os
import json
import uuid
import time
# FIX 1: Removed 'import datetime' module to prevent collision with 'from datetime import datetime'
import logging
import httpx
import asyncio
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

from src.remote_services.google_core import GoogleCalendarClient
from src.remote_services.wallet_service import WalletClient
from src.agents.calendar_agent import perform_calendar_auth_check

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
# --- 3. Remote Services & Wallet Logic moved to src/remote_services/ ---

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
    calendar_service = GoogleCalendarClient(
        tenant_id, 
        request.app.state.http_client, 
        correlation_id=corr_id, 
        thread_id=req.thread_id or "default",
        access_token=auth.get("token")
    )
    wallet_service = WalletClient(tenant_id, request.app.state.http_client)
    
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
    
    # Only trigger Google Pre-flight if it is EXPLICITLY external (Google Calendar)
    # MatterMiner Core events (e.g. "schedule a strategy meeting") do NOT require Google OAuth
    if is_calendar_intent and is_explicit_google and not is_login_attempt:
        # Replaces the inline OAuth handshake with a call to the specialized Calendar Agent
        auth_response = await perform_calendar_auth_check(calendar_service, tenant_id, req.history)
        if auth_response:
            return auth_response

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

	# DROP-IN PRE-FLIGHT WALLET CHECK
    # wallet_check = await wallet_service.check_balance(auth_headers=calendar_service.headers)
    '''
    if wallet_check and wallet_check.get("allowed") is False:
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

    services = {"calendar": calendar_service, "wallet": wallet_service}

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
                "2. PERSISTENCE: Data in VAULT is already in the database. Continue until success.\n"
                "3. CONTROLLED INTAKE: To create a Contact, Client, or Event, you MUST call the respective creation tool IMMEDIATELY with whatever data you have. DO NOT gather all data yourself. NEVER call both Google and MatterMiner tools in the same turn for the same intent.\n"
                "4. ONE QUESTION: When collecting data, ask for EXACTLY ONE field at a time as instructed by the tool's response_instruction.\n"
            )
        }
        
        # Combine messages with dynamic state injection
        loop_messages = [messages[0], state_injection] + messages[1:]

        logger.info(f"[AGENT-LOOP] Iteration {i} | Vault: {vault_str}")

        # --- RATE LIMIT PROTECTION: Backoff + Throttling ---
        if i > 0: await asyncio.sleep(0.5) 
        
        async def _call_openai():
            return await ai_client.chat.completions.create(
                model="gpt-4o-mini", messages=loop_messages, tools=TOOLS, tool_choice="auto"
            )
        
        # Wrapped in backoff to handle 429s gracefully
        from src.utils import retry_with_backoff 
        response = await retry_with_backoff(retries=2, backoff_in_seconds=2)(_call_openai)()
        
        assistant_msg = response.choices[0].message
        
        # Track token usage asynchronously
        if hasattr(response, 'usage'):
            asyncio.create_task(wallet_service.update_usage(response.usage, auth_headers=calendar_service.headers))
        
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
        pending_throttle_msg = None
        for tool_call in assistant_msg.tool_calls:
            # Dispatch directly to Agent Manager
            result = await execute_tool_call(
                tool_call, services, user_role, tenant_id, messages, 
                user_email=user_email, user_tz=user_tz
            )
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
                # Save the instruction but proceed so that OTHER tool calls in the same turn get a response message
                if result.get("status") == "partial_success":
                    logger.info(f"[THROTTLE] Queueing partial_success for {tool_call.function.name}")
                    pending_throttle_msg = result.get("message")

                if result.get("_exit_loop") and result.get("status") == "success":
                    terminal_success_msg = result.get("message")
                elif result.get("status") in ["success", "partial_success"] or result.get("_continue_chaining"):
                    last_action = f"Executed {tool_call.function.name}: {result.get('message', 'Processed')}"
                else:
                    last_action = f"Failed {tool_call.function.name}: {result.get('message', 'Unknown error')}"

        # Finalize Response Logic
        if pending_throttle_msg:
             final_db = await services['calendar'].get_client_session(tenant_id)
             return {"response": pending_throttle_msg, "history": messages[1:], "vault_data": final_db}

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
    calendar_service = GoogleCalendarClient(
        tenant_id, 
        request.app.state.http_client, 
        correlation_id=corr_id, 
        thread_id=req.thread_id or "default",
        access_token=auth.get("token")
    )
    wallet_service = WalletClient(tenant_id, request.app.state.http_client)
    ai_client = request.app.state.ai_client
    user_tz = auth.get("timezone", "UTC")
    services = {"calendar": calendar_service, "wallet": wallet_service}

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

    # 2. Pre-LLM Auth Guard – only for explicit Google Calendar requests
    if is_calendar_intent and is_explicit_google and not is_login_attempt:
        # Replaces the old inline gate with the specialist agent's auth check
        auth_response = await perform_calendar_auth_check(calendar_service, tenant_id, req.history)
        if auth_response:
             async def _auth_stream_gen():
                 yield f"data: {json.dumps(auth_response)}\n\n"
             return StreamingResponse(_auth_stream_gen(), media_type="text/event-stream")

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
                    model="gpt-4o-mini", messages=loop_messages, tools=TOOLS, tool_choice="auto", stream=True
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
            pending_throttle_msg = None

            for tool_call_data in assistant_msg_dict["tool_calls"]:
                # Mock a tool-call object for execute_tool_call
                class MockTool:
                    def __init__(self, d):
                        self.id = d["id"]
                        self.function = type('obj', (object,), d["function"])
                
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
                    
                    # --- OPTIMIZATION: QUEUE SHORT-CIRCUIT FOR DATA COLLECTION ---
                    if result.get("status") == "partial_success":
                        logger.info(f"[STREAM-THROTTLE] Queueing partial_success for {tool_name}")
                        pending_throttle_msg = result.get("message")
                    
                    if result.get("_exit_loop") and result.get("status") == "success":
                        terminal_success_msg = result.get("message")
                    last_action = f"Executed {tool_name}"

            if pending_throttle_msg:
                yield f"data: {json.dumps({'content': pending_throttle_msg, 'action': None})}\n\n"
                yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"
                return

            if terminal_success_msg:
                final_text = f"\n\n{terminal_success_msg}"
                yield f"data: {json.dumps({'content': final_text, 'action': None})}\n\n"
                messages.append({"role": "assistant", "content": terminal_success_msg})
                yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"
                return

        # Fallback completion
        yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- 7. Utility Route for Google Sync ---
@app.post("/ai/sync")
async def trigger_sync(request: Request, auth: dict = Depends(verify_tenant_access)):
    calendar = GoogleCalendarClient(
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
