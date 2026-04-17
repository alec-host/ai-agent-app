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
import jwt
from jwt.exceptions import PyJWTError

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

# --- AGENT CONCURRENCY & TOOL MOCKS (DEP-02, CQ-04) ---
# Global Semaphore to prevent Token-Per-Minute (TPM) exhaustion
_tpm_guard = asyncio.Semaphore(5) 

class MockTool:
    """Mock a tool-call object for execute_tool_call (DEP-02 optimization)"""
    def __init__(self, d):
        self.id = d["id"]
        self.function = type('obj', (object,), d["function"])

from src.agent_manager import execute_tool_call, get_rehydration_context
from src.utils import sanitize_history, retry_with_backoff, get_starter_chips, standardize_response

from src.remote_services.google_core import GoogleCalendarClient
from src.remote_services.wallet_service import WalletClient
from src.agents.calendar_agent import perform_calendar_auth_check
from src.agents.memory_agent import extract_and_save_facts, summarize_and_save

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
        verify=settings.TLS_VERIFY,  # SEC-03: TLS verification enabled by default
        timeout=httpx.Timeout(15.0)
    )
    
    app.state.ai_client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    logger.info(f"--- {settings.APP_NAME} Started Successfully ---")
    
    yield  # --- The app runs here ---

    # [SHUTDOWN LOGIC]
    logger.info(f"--- {settings.APP_NAME} Shutting Down ---")
    await app.state.http_client.aclose()
    logger.info("Resources cleaned up. Goodbye.")

app = FastAPI(title=settings.APP_NAME,description=settings.APP_DESCRIPTION, lifespan=lifespan)

# --- 0. CORS & Middleware ---
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ALLOWED_ORIGINS,  # SEC-02: Explicit allowlist replaces wildcard
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO)

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
    
    # SEC-05: Cryptographic JWT Verification
    jwt_user_email = None
    if settings.JWT_ENABLED:
        if not token:
            logger.warning(f"[AUTH-GUARD] Missing Authorization token for tenant {x_tenant_id}")
            raise HTTPException(status_code=401, detail="Authentication token is required.")
        
        try:
            # Decode and verify the JWT
            payload = jwt.decode(
                token, 
                settings.JWT_SECRET, 
                algorithms=[settings.JWT_ALGORITHM],
                audience=settings.JWT_AUDIENCE
            )
            
            # Verify Tenant Matching (Integrity check)
            token_tenant_id = payload.get("tenant_id")
            if token_tenant_id and str(token_tenant_id) != str(x_tenant_id):
                logger.error(f"[AUTH-GUARD] Tenant Mismatch: Header={x_tenant_id}, Token={token_tenant_id}")
                raise HTTPException(status_code=403, detail="Tenant ID mismatch in token.")
            
            jwt_user_email = payload.get("email")
            logger.info(f"[AUTH-GUARD] JWT Verified for tenant {x_tenant_id} (User: {jwt_user_email or 'unknown'})")
            
        except jwt.ExpiredSignatureError:
            logger.warning(f"[AUTH-GUARD] Expired token for tenant {x_tenant_id}")
            raise HTTPException(status_code=401, detail="Token has expired.")
        except PyJWTError as e:
            logger.error(f"[AUTH-GUARD] JWT Validation failed: {str(e)}")
            raise HTTPException(status_code=401, detail="Invalid authentication token.")
    else:
        # Fallback for dev mode where JWT is disabled
        if token:
            logger.info(f"[AUTH-GUARD] JWT disabled, skipping verification for tenant {x_tenant_id}")
        else:
            logger.warning(f"[AUTH-GUARD] No Header Token provided for tenant {x_tenant_id}")

    return {
        "tenant_id": x_tenant_id, 
        "role": user_role.lower(), 
        "timezone": x_user_timezone,
        "token": token,
        "user_email": x_user_email or jwt_user_email
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

# --- 5.1. Dynamic Model Routing ---
def get_optimal_model(active_workflow: str, user_message: str) -> str:
    """Routes simple workflows to mini models and complex ones to large models."""
    if active_workflow in ["create_matter", "rag_query", "search_knowledge_base", "lookup_firm_protocol"]:
        return "gpt-4o"
        
    complex_keywords = ["policy", "guideline", "summarize", "research", "analyze"]
    if user_message and any(k in user_message.lower() for k in complex_keywords):
        return "gpt-4o"
        
    return "gpt-4o-mini"

# --- 6. The Core Reasoning Endpoint ---
@app.post("/ai/chat")
async def handle_agent_query(req: ChatRequest, request: Request, auth: dict = Depends(verify_tenant_access)):
    user_prompt_raw = req.prompt.lower().strip()
    if user_prompt_raw in ["clear", "reset", "/clear"]:
        from src.remote_services.redis_memory import RedisMemoryClient
        redis_c = RedisMemoryClient(auth["tenant_id"], req.thread_id or "default")
        await redis_c.clear_history()
        await redis_c.close()
        return standardize_response({"response": "Conversation history cleared.", "history": []})

    tenant_id, user_role, corr_id = auth["tenant_id"], auth["role"], request.state.correlation_id
    user_email = auth.get("user_email")
    calendar_service = GoogleCalendarClient(
        tenant_id, 
        request.app.state.http_client, 
        correlation_id=corr_id, 
        thread_id=req.thread_id or "default",
        access_token=auth.get("token"),
        user_email=user_email
    )
    wallet_service = WalletClient(tenant_id, request.app.state.http_client)
    
    # --- 1. HISTORY CLEANUP & REDIS MEMORY ---
    from src.remote_services.redis_memory import RedisMemoryClient
    redis_memory = RedisMemoryClient(tenant_id, req.thread_id or "default", user_email=user_email)
    
    # [PROTOCOL-SC] Tracking reasoning deltas for server-side state consistency
    turn_deltas = []
    
    if req.history and len(req.history) > 0:
        cleaned_history = [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in req.history]
    else:
        # [PROTOCOL-SC] Rehydrate from Redis with Session Isolation Marker
        from src.utils import compress_reasoning_history
        raw_history = await redis_memory.get_history()
        
        # If Redis has history, we inject a marker to separate this new session from legacy logs
        if raw_history:
            session_marker = {"role": "system", "content": f"--- [NEW CONVERSATION STARTED AT {datetime.now(timezone.utc).strftime('%Y-%m-%d %I:%M %p UTC')}] ---"}
            cleaned_history = compress_reasoning_history(raw_history, keep_reasoning_turns=2)
            cleaned_history.append(session_marker)
            # Sync the marker back to Redis so later turns in THIS session know where it started
            await redis_memory.append_messages([session_marker])
            logger.info(f"[SESSION] [{tenant_id}] Rehydrated history and injected session marker.")
        else:
            cleaned_history = []
            logger.info(f"[SESSION] [{tenant_id}] Fresh start - no legacy history found.")
    
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

    # Only trigger Google Pre-flight if it is EXPLICITLY external (Google Calendar)
    # MatterMiner Core events (e.g. "schedule a strategy meeting") do NOT require Google OAuth
    if is_calendar_intent and is_explicit_google:
        # Replaces the inline OAuth handshake with a call to the specialized Calendar Agent
        auth_response = await perform_calendar_auth_check(calendar_service, tenant_id, req.history)
        if auth_response:
            return standardize_response(auth_response, cleaned_history)

    # Phase 2 (Auth Migration): Core pre-flight login gate REMOVED.
    # MatterMiner Core now authenticates via static API key (CORE_API_KEY) at the transport layer.
    # No has_valid_token() check or login card surfacing needed.

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
    services = {"calendar": calendar_service, "wallet": wallet_service}

    # --- 2. CONTEXT REHYDRATION (Source of Truth) ---
    # This fetches the latest saved state from the DB to prevent looping
    rehydration_data = await get_rehydration_context(tenant_id, services, user_email=user_email)
    
    user_tz = auth.get("timezone", "UTC")
    messages = [{"role": "system", "content": get_legal_system_prompt(tenant_id, user_role, user_tz, settings.SUPPORTED_TIMEZONES)}]
    
    if rehydration_data:
        messages[0]["content"] += f"\n\n{rehydration_data.get('injection', '')}"

    messages.extend(sanitize_history(cleaned_history, redact_values=[user_email, tenant_id]))
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
             logger.info(f"[{tenant_id}] Turn {i}: Active calendar workflow + intent. Relying on downstream Token Auth.")
        
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
             
        matter_draft = metadata.get("matter_draft", {})
        if matter_draft and active_workflow == "matter":
             vault_segments.append(f"MATTER_DRAFT: {matter_draft}")

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
                "4. ONE QUESTION: When collecting data, ask for EXACTLY ONE field at a time as instructed by the tool's response_instruction. When the user responds, you MUST map their input to that specific field and call the tool immediately.\n"
                "5. MAPPING SANITY: When calling tools with multiple parameters, double-check that 'names' are mapped to name fields and 'numbers/codes' are mapped to numeric/code fields. DO NOT swap them (e.g. putting a dialling code in a name field).\n"
            )
        }
        
        # Combine messages with dynamic state injection
        loop_messages = [messages[0], state_injection] + messages[1:]

        logger.info(f"[AGENT-LOOP] Iteration {i} | Vault: {vault_str}")

        # --- RATE LIMIT PROTECTION: Backoff + Throttling ---
        if i > 0: await asyncio.sleep(0.5) 
        
        # --- 2. PRE-FLIGHT OPTIMIZATION: PRUNING & FILTRATION ---
        active_wf = (rehydration_data.get("metadata", {}) if rehydration_data else {}).get("active_workflow")
        
        # A. History Pruning (Keep System Prompt + Last 10 Turns)
        # Using the safe utility to ensure tool calls are never separated from results
        from src.utils import compress_reasoning_history
        loop_messages = [messages[0]] + compress_reasoning_history(messages[1:], keep_reasoning_turns=2)
        
        if len(messages) != len(loop_messages):
            logger.info(f"[Token-Guard] Safely pruned conversation context from {len(messages)} to {len(loop_messages)} messages.")
        else:
            loop_messages = messages

        # B. Tool Filtration (Dynamic Context)
        # Only send tools relevant to the current workflow to save ~1k tokens/turn.
        relevant_tools = TOOLS
        if active_wf:
            from .tools import TOOLS as ALL_TOOLS
            google_funcs = ["schedule_event", "initialize_calendar_session", "check_calendar_connection"]
            core_funcs = ["create_standard_event", "create_all_day_event", "create_contact", "create_client_record", "search_contact_by_email", "lookup_countries", "lookup_client", "lookup_practice_area", "lookup_case_stage", "lookup_billing_type", "create_matter"]
            
            if active_wf == "google_calendar":
                relevant_tools = [t for t in ALL_TOOLS if t["function"]["name"] in google_funcs or t["function"]["name"] == "get_system_status"]
            elif active_wf in ["standard_event", "all_day_event", "contact", "client", "matter"]:
                relevant_tools = [t for t in ALL_TOOLS if t["function"]["name"] in core_funcs or t["function"]["name"] == "get_system_status"]
            
            logger.info(f"[Token-Guard] Filtered toolset to {len(relevant_tools)} items for workflow: {active_wf}")

        # --- 3. CALL LLM WITH CONCURRENCY GUARD ---        
        async def _call_openai():
            async with _tpm_guard:
                selected_model = get_optimal_model(active_wf, req.prompt)
                logger.info(f"[ROUTER] Sending payload to model: {selected_model}")
                return await ai_client.chat.completions.create(
                    model=selected_model, messages=loop_messages, tools=relevant_tools, tool_choice="auto"
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
        turn_deltas.append(assistant_dict) # [PROTOCOL-SC] Capture reasoning delta

        if not assistant_msg.tool_calls:
            # --- FINAL RESPONSE DECORATION ---
            final_payload = {"response": assistant_msg.content, "history": messages[1:]}
            
            # If this is the start of a conversation and no data exists, suggest actions
            if len(cleaned_history) <= 1 and not (rehydration_data and rehydration_data.get("has_data")):
                final_payload["suggested_actions"] = get_starter_chips(metadata if 'metadata' in locals() else {})
            
            # --- BACKGROUND: MEMORY OPERATIONS (FACTS & SUMMARY) ---
            asyncio.create_task(extract_and_save_facts(tenant_id, messages, services, ai_client, user_email=user_email))
            asyncio.create_task(summarize_and_save(tenant_id, messages, services, ai_client, user_email=user_email))
            
            # [PROTOCOL-SC] Final context save for linear (non-tool) response
            await redis_memory.append_messages([{"role": "user", "content": req.prompt}, {"role": "assistant", "content": assistant_msg.content}])
            await redis_memory.close()
            
            return standardize_response(final_payload, messages[1:])

        # --- 4. EXECUTE TOOL CALLS ---
        terminal_success_msg = None
        pending_throttle_msg = None
        for tool_call in assistant_msg.tool_calls:
            # Dispatch directly to Agent Manager
            result = await execute_tool_call(
                tool_call, services, user_role, tenant_id, messages, 
                user_email=user_email, user_tz=user_tz, ai_client=ai_client
            )
            from src.utils import compact_tool_result
            tool_msg = {"role": "tool", "tool_call_id": tool_call.id, "name": tool_call.function.name, "content": json.dumps(result)}
            messages.append(tool_msg)
            
            # [PROTOCOL-SC] Capture compacted reasoning delta
            turn_deltas.append({**tool_msg, "content": compact_tool_result(result)})
            
            if isinstance(result, dict):
                # Detect Terminal Success (Gives us direct control over the final output)

                
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
             # [PROTOCOL-SC] Atomic Save of entire turn sequence
             # We include ALL reasoning steps to satisfy OpenAI's conversation requirements
             turn_messages = [{"role": "user", "content": req.prompt}] + turn_deltas
             if pending_throttle_msg:
                 turn_messages.append({"role": "assistant", "content": pending_throttle_msg})
             
             await redis_memory.append_messages(turn_messages)
             await redis_memory.close()
             return standardize_response({"response": pending_throttle_msg or "", "history": messages[1:], "vault_data": final_db})

        if terminal_success_msg and not assistant_msg.content:
             final_db = await services['calendar'].get_client_session(tenant_id)
             # [PROTOCOL-SC] Atomic Save of entire turn sequence
             await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas + [{"role": "assistant", "content": terminal_success_msg}])
             await redis_memory.close()
             return standardize_response({"response": terminal_success_msg, "history": messages[1:], "vault_data": final_db})
        elif terminal_success_msg:
             # If assistant already had words, append the success table to it
             final_resp = f"{assistant_msg.content}\n\n{terminal_success_msg}"
             final_db = await services['calendar'].get_client_session(tenant_id)
             # [PROTOCOL-SC] Atomic Save of entire turn sequence
             await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas + [{"role": "assistant", "content": final_resp}])
             await redis_memory.close()
             return standardize_response({"response": final_resp, "history": messages[1:], "vault_data": final_db})

    # --- BACKGROUND: MEMORY OPERATIONS (FACTS & SUMMARY) ---
    asyncio.create_task(extract_and_save_facts(tenant_id, messages, services, ai_client))
    asyncio.create_task(summarize_and_save(tenant_id, messages, services, ai_client))
    
    final_db = await services['calendar'].get_client_session(tenant_id)
    final_response_text = messages[-1].get("content")
    
    if final_response_text:
        # [PROTOCOL-SC] Atomic Save of entire turn sequence
        await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas)
        
    await redis_memory.close()
    
    return standardize_response({"response": final_response_text, "history": messages[1:], "vault_data": final_db})

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
        access_token=auth.get("token"),
        user_email=user_email
    )
    wallet_service = WalletClient(tenant_id, request.app.state.http_client)
    ai_client = request.app.state.ai_client
    user_tz = auth.get("timezone", "UTC")
    services = {"calendar": calendar_service, "wallet": wallet_service}

    # Setup Redis Memory with User Isolation
    from src.remote_services.redis_memory import RedisMemoryClient
    redis_memory = RedisMemoryClient(tenant_id, req.thread_id or "default", user_email=user_email)
    
    # [PROTOCOL-SC] Tracking reasoning deltas for server-side state consistency
    turn_deltas = []
    
    if req.history and len(req.history) > 0:
        cleaned_history = [m.model_dump() if hasattr(m, 'model_dump') else m.dict() for m in req.history]
    else:
        # [PROTOCOL-SC] Rehydrate with Session Isolation Marker (Streaming)
        from src.utils import compress_reasoning_history
        raw_history = await redis_memory.get_history()
        if raw_history:
            session_marker = {"role": "system", "content": f"--- [NEW CONVERSATION STARTED AT {datetime.now(timezone.utc).strftime('%Y-%m-%d %I:%M %p UTC')}] ---"}
            cleaned_history = compress_reasoning_history(raw_history, keep_reasoning_turns=2)
            cleaned_history.append(session_marker)
            await redis_memory.append_messages([session_marker])
            logger.info(f"[STREAM-SESSION] [{tenant_id}] Rehydrated history with session marker.")
        else:
            cleaned_history = []

    # --- 0. PROGRAMMATIC INTENT GATE (PRE-LLM) ---
    user_prompt_raw = req.prompt.lower().strip()
    
    # 1. Define Boolean Flags
    calendar_keywords = [
        "schedule", "event", "meeting", "book", "appointment", "calendar",
        "set up a meeting", "setup a meeting", "arrange a meeting",
        "organize a meeting", "reschedule", "deposition"
    ]
    is_calendar_intent = any(kw in user_prompt_raw for kw in calendar_keywords)

    is_explicit_google = any(kw in user_prompt_raw for kw in ["google", "personal", "external"])
    is_explicit_core = any(kw in user_prompt_raw for kw in ["matter", "firm", "internal", "deadline", "filing"])

    # 2. Pre-LLM Auth Guard – only for explicit Google Calendar requests
    if is_calendar_intent and is_explicit_google:
        # Replaces the old inline gate with the specialist agent's auth check
        auth_response = await perform_calendar_auth_check(calendar_service, tenant_id, req.history)
        if auth_response:
             async def _auth_stream_gen():
                 yield f"data: {json.dumps(auth_response)}\n\n"
             return StreamingResponse(_auth_stream_gen(), media_type="text/event-stream")

    # Phase 2 (Auth Migration): Core pre-flight login gate REMOVED.
    # MatterMiner Core now authenticates via static API key (CORE_API_KEY) at the transport layer.
    # No has_valid_token() check or login card surfacing needed.

    # Standard check for CLEAR
    if user_prompt_raw in ["clear", "reset", "/clear"]:
        async def clear_gen():
            yield f"data: {json.dumps(standardize_response({'content': 'Conversation history cleared.', 'done': True, 'history': []}))}\n\n"
        return StreamingResponse(clear_gen(), media_type="text/event-stream")

    rehydration_data = await get_rehydration_context(tenant_id, services)
    messages = [{"role": "system", "content": get_legal_system_prompt(tenant_id, user_role, user_tz, settings.SUPPORTED_TIMEZONES)}]
    if rehydration_data:
        messages[0]["content"] += f"\n\n{rehydration_data.get('injection', '')}"
    messages.extend(sanitize_history(cleaned_history, redact_values=[user_email, tenant_id]))
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
                     logger.info(f"[STREAM] [{tenant_id}] Turn {i}: Active calendar workflow + intent. Relying on downstream Token Auth.")
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
                selected_model = get_optimal_model(active_workflow, req.prompt)
                # logger already handled above, no need to spam stream
                return await ai_client.chat.completions.create(
                    model=selected_model, messages=loop_messages, tools=TOOLS, tool_choice="auto", stream=True
                )
            
            try:
                from src.utils import retry_with_backoff
                stream = await retry_with_backoff(retries=2, backoff_in_seconds=2)(_call_openai_stream)()
            except Exception as e:
                logger.error(f"[STREAM] OpenAI error: {e}", exc_info=True)
                yield f"data: {json.dumps(standardize_response({'content': 'An internal processing error occurred. Please try again.', 'done': True}, messages[1:]))}\n\n"
                return

            full_content = ""
            current_tool_calls = {} # index -> call
            content_buffer = "" # Optimization: Batch small tokens

            logger.info(f"[STREAM] Iterating over chunks...")
            async for chunk in stream:
                if not chunk.choices: continue
                delta = chunk.choices[0].delta
                
                # Stream Content with Batching
                if delta.content:
                    full_content += delta.content
                    content_buffer += delta.content
                    
                    # Yield when buffer reaches certain size to reduce packet-per-token overhead
                    if len(content_buffer) >= 20: 
                        yield f"data: {json.dumps({'content': content_buffer})}\n\n"
                        content_buffer = ""

                # Accumulate Tool Calls
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in current_tool_calls:
                            current_tool_calls[idx] = {"id": tc.id, "function": {"name": tc.function.name, "arguments": ""}}
                        if tc.function.arguments:
                            current_tool_calls[idx]["function"]["arguments"] += tc.function.arguments

            # Flush remaining content buffer after loop
            if content_buffer:
                yield f"data: {json.dumps({'content': content_buffer})}\n\n"

            # Finalize this round of streaming
            if not current_tool_calls:
                logger.info(f"[STREAM] Response complete. Full Content Length: {len(full_content)}")
                messages.append({"role": "assistant", "content": full_content})
                # Terminal check: suggest actions
                final_payload = {"done": True, "history": messages[1:]}
                if len(cleaned_history) <= 1 and not (rehydration_data and rehydration_data.get("has_data")):
                    final_payload["suggested_actions"] = get_starter_chips(metadata if 'metadata' in locals() else {})
                # --- BACKGROUND: MEMORY OPERATIONS (FACTS & SUMMARY) ---
                asyncio.create_task(extract_and_save_facts(tenant_id, messages, services, ai_client))
                asyncio.create_task(summarize_and_save(tenant_id, messages, services, ai_client))
                
                yield f"data: {json.dumps(standardize_response(final_payload, messages[1:]))}\n\n"
                
                # [PROTOCOL-SC] Final context save for linear (non-tool) response
                await redis_memory.append_messages([{"role": "user", "content": req.prompt}, {"role": "assistant", "content": full_content}])
                await redis_memory.close()
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
            turn_deltas.append(assistant_msg_dict) # [PROTOCOL-SC] Capture reasoning delta
            terminal_success_msg = None
            pending_throttle_msg = None

            for tool_call_data in assistant_msg_dict["tool_calls"]:
                tool_name = tool_call_data['function']['name']
                yield f"data: {json.dumps({'action': f'Executing {tool_name}...'})}\n\n"
                
                try:
                    result = await execute_tool_call(MockTool(tool_call_data), services, user_role, tenant_id, messages, user_email=user_email, ai_client=ai_client)
                except Exception as tool_err:
                    logger.error(f"[STREAM-TOOL-ERROR] {tool_name} failed: {tool_err}", exc_info=True)
                    result = {"status": "error", "message": "The internal tool dispatcher encountered a failure."}

                from src.utils import compact_tool_result
                tool_msg = {"role": "tool", "tool_call_id": tool_call_data["id"], "name": tool_name, "content": json.dumps(result)}
                messages.append(tool_msg)
                
                # [PROTOCOL-SC] Capture compacted reasoning delta
                turn_deltas.append({**tool_msg, "content": compact_tool_result(result)})
                
                if isinstance(result, dict):

                    
                    # --- OPTIMIZATION: QUEUE SHORT-CIRCUIT FOR DATA COLLECTION ---
                    if result.get("status") == "partial_success":
                        logger.info(f"[STREAM-THROTTLE] Queueing partial_success for {tool_name}")
                        pending_throttle_msg = result.get("message")
                    
                    if result.get("_exit_loop") and result.get("status") == "success":
                        terminal_success_msg = result.get("message")
                    last_action = f"Executed {tool_name}"

            if pending_throttle_msg:
                # --- BACKGROUND: MEMORY OPERATIONS (FACTS & SUMMARY) ---
                asyncio.create_task(extract_and_save_facts(tenant_id, messages, services, ai_client))
                asyncio.create_task(summarize_and_save(tenant_id, messages, services, ai_client))
                
                # [PROTOCOL-SC] Atomic Save of entire turn sequence
                # Correctly include tool results to prevent BadRequestErrors
                await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas + [{"role": "assistant", "content": pending_throttle_msg}])
                await redis_memory.close()
                
                yield f"data: {json.dumps(standardize_response({'content': pending_throttle_msg, 'action': None}, messages[1:]))}\n\n"
                yield f"data: {json.dumps(standardize_response({'done': True, 'history': messages[1:]}))}\n\n"
                return

            if terminal_success_msg:
                # --- BACKGROUND: MEMORY OPERATIONS (FACTS & SUMMARY) ---
                asyncio.create_task(extract_and_save_facts(tenant_id, messages, services, ai_client, user_email=user_email))
                asyncio.create_task(summarize_and_save(tenant_id, messages, services, ai_client, user_email=user_email))
                
                final_text = f"\n\n{terminal_success_msg}"
                yield f"data: {json.dumps({'content': final_text})}\n\n"
                yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"
                
                # [PROTOCOL-SC] Atomic Save
                await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas + [{"role": "assistant", "content": final_text}])
                await redis_memory.close()
                return

            # --- [WORLD CLASS] GLOBAL COMMUNICATION AUDITOR (FALLBACK) ---
            # If the tool call provided a response instruction but no textual content was streamed,
            # we force a yield of the instruction to prevent the "Empty Bubble" hang.
            if pending_throttle_msg and not full_content:
                logger.warning(f"[AUDITOR] [{tenant_id}] Detected SILENT TURN after tool call. Yielding fallback message.")
                # The auditor uses the enrichment already provided by run_draft_workflow via the tool result
                yield f"data: {json.dumps(standardize_response({
                    'content': pending_throttle_msg, 
                    'vault_data': await services['calendar'].get_client_session(tenant_id)
                }))}\n\n"

        # --- FINAL FALLBACK: If loop ends without completion ---
        yield f"data: {json.dumps({'done': True, 'history': messages[1:]})}\n\n"                
                # [PROTOCOL-SC] Atomic Save of entire turn sequence
                await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas + [{"role": "assistant", "content": f"{full_content or ''}{final_text}"}])
                await redis_memory.close()
                
                yield f"data: {json.dumps(standardize_response({'content': final_text, 'action': None}, messages[1:]))}\n\n"
                messages.append({"role": "assistant", "content": terminal_success_msg})
                yield f"data: {json.dumps(standardize_response({'done': True, 'history': messages[1:]}))}\n\n"
                return

        # --- BACKGROUND: MEMORY OPERATIONS (FACTS & SUMMARY) ---
        asyncio.create_task(extract_and_save_facts(tenant_id, messages, services, ai_client, user_email=user_email))
        asyncio.create_task(summarize_and_save(tenant_id, messages, services, ai_client, user_email=user_email))
        
        if full_content:
            # [PROTOCOL-SC] Atomic Save of entire turn sequence
            await redis_memory.append_messages([{"role": "user", "content": req.prompt}] + turn_deltas)
            
        await redis_memory.close()
        
        # Fallback completion
        yield f"data: {json.dumps(standardize_response({'done': True, 'history': messages[1:]}))}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# --- 7. Utility Route for Google Sync ---
@app.post("/ai/sync")
async def trigger_sync(request: Request, auth: dict = Depends(verify_tenant_access)):
    calendar = GoogleCalendarClient(
        auth["tenant_id"], 
        request.app.state.http_client, 
        correlation_id=request.state.correlation_id,
        access_token=auth.get("token"),
        user_email=auth["user_email"]
    )
    result = await calendar.sync_all_events()
    return standardize_response({"response": result})

# --- 8. Redis Diagnostic Health Route ---
@app.get("/ai/redis-health")
async def check_redis_health(auth: dict = Depends(verify_tenant_access)):
    """
    Diagnostic Endpoint: Explicitly tests the Redis Memory pipeline within the exact
    FastAPI Execution environmental constraints (Network, Async Event Loop, Auth).
    """
    from src.remote_services.redis_memory import RedisMemoryClient
    import traceback
    
    tenant_id = auth["tenant_id"]
    test_thread = "health_test_thread"
    
    redis_memory = RedisMemoryClient(tenant_id, test_thread)
    
    try:
        # Step 1: Ping Data
        pong = await redis_memory.redis.ping()
        
        # Step 2: Clear any old test data
        await redis_memory.clear_history()
        
        # Step 3: Append 2 items simultaneously
         # NOTE: We ensure explicit strings just like the main chat loop
        await redis_memory.append_messages([
            {"role": "user", "content": "health_ping"},
            {"role": "assistant", "content": "health_pong"}
        ])
        
        # Step 4: Retrieve List
        memory_state = await redis_memory.get_history()
        
        # Set 5: Clean it
        await redis_memory.clear_history()
        await redis_memory.close()
        
        return {
            "status": "Healthy",
            "ping_response": pong,
            "memory_retrieved": len(memory_state),
            "memory_payload": memory_state,
            "connected_to": {
                "host": settings.REDIS_HOST,
                "port": settings.REDIS_PORT
            }
        }
    except Exception as e:
        error_trace = traceback.format_exc()
        return {
            "status": "Degraded or Offline",
            "error_type": str(type(e).__name__),
            "error_message": str(e),
            "traceback": error_trace,
            "connected_to": {
                "host": settings.REDIS_HOST,
                "port": settings.REDIS_PORT
            }
        }

@app.middleware("http")
async def add_correlation_id(request: Request, call_next):
    cid = request.headers.get("X-Correlation-ID", str(uuid.uuid4()))
    request.state.correlation_id = cid
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = cid
    return response

# --- Demo UI fallback ---
app.mount("/", StaticFiles(directory="demo-ui", html=True), name="ui")
