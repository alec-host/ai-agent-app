# src/utils.py

import json
import logging
import asyncio
import functools
from src.logger import logger

def sanitize_history(history: list, max_content_length: int = 2000, keep_last_n: int = 3):
    """
    Truncates older message content to save tokens, but STRICTLY PRESERVES 
    the most recent messages to ensure immediate context and JSON validity.
    """
    sanitized = []
    total_msgs = len(history)

    for i, msg in enumerate(history):
        # 1. Convert Pydantic objects to dicts safely
        if hasattr(msg, 'model_dump'):
            msg_dict = msg.model_dump(exclude_none=True)
        elif hasattr(msg, 'dict'):
            msg_dict = msg.dict(exclude_none=True)
        else:
            msg_dict = dict(msg)
        
        # 2. Handle 'tool_calls' (Preserve metadata)
        if "tool_calls" in msg_dict and msg_dict["tool_calls"]:
            raw_calls = msg_dict["tool_calls"]
            msg_dict["tool_calls"] = [
                (tc.model_dump() if hasattr(tc, 'model_dump') else tc) 
                for tc in raw_calls
            ]
        
        # 3. Explicitly preserve Tool identity fields
        if msg_dict.get("role") == "tool":
            # Ensure tool_call_id and name are present if they were in the original msg
            if not msg_dict.get("tool_call_id") and "tool_call_id" in msg_dict:
                 pass # model_dump with exclude_none might have removed it if it was None, but for tools it shouldn't be None
            
            # OpenAI requires 'content' to be a string (even empty) for tool role
            if msg_dict.get("content") is None:
                msg_dict["content"] = ""

        # 4. SMART TRUNCATION
        # We NEVER truncate the last 'n' messages. 
        # This ensures the AI always sees the full "Pending Task" injection and the User's latest prompt.
        is_recent = i >= (total_msgs - keep_last_n)
        
        content = msg_dict.get("content")
        
        if isinstance(content, str) and not is_recent:
            if len(content) > max_content_length:
                # Keep the beginning (summary/status) and cut the rest
                msg_dict["content"] = content[:max_content_length] + f" ... [Truncated: {len(content) - max_content_length} chars]"
        
        sanitized.append(msg_dict)
        
    return sanitized
    
def retry_with_backoff(retries=3, backoff_in_seconds=1):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            x = 0
            while True:
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    if x == retries:
                        # If we've exhausted retries, raise the error to be 
                        # caught by the Service Client's main try/except
                        raise e
                    
                    sleep = (backoff_in_seconds * 2 ** x)
                    logger.warning(f"Retrying in {sleep}s due to: {str(e)}")
                    await asyncio.sleep(sleep)
                    x += 1
        return wrapper
    return decorator

logger = logging.getLogger("legal-agentic-ai")

async def get_rehydration_context(tenant_id, services):
    """
    Fetches the persisted session from Node.js and returns 
    a system-ready injection string for the AI.
    """
    try:
        calendar_service = services.get("calendar")
        if not calendar_service:
            return None

        # Use the hardened helper to ensure API envelopes are unwrapped
        resp = await calendar_service.get_client_session(tenant_id)
        if not resp or not isinstance(resp, dict):
            return None

        # 0. LIFECYCLE CHECK
        metadata = resp.get("metadata", {})
        lifecycle = metadata.get("session_lifecycle", "active")
        if lifecycle == "completed":
            logger.info(f"[REHYDRATION] Skipping completed session for tenant: {tenant_id}")
            return None

        # 1. CLIENT VAULT
        vault_state = {k: v for k, v in {
            "client_number": resp.get("client_number"),
            "client_type": resp.get("client_type"),
            "first_name": resp.get("first_name"),
            "last_name": resp.get("last_name"), 
            "email": resp.get("email")
        }.items() if v}

        # 3. OAUTH RE-HYDRATION DETECTION
        # Check if we just returned from OAuth (Status Ready + Existing Draft)
        metadata = resp.get("metadata", {})
        event_draft = metadata.get("event_draft", {})
        active_workflow = metadata.get("active_workflow")
        
        auth_status = await calendar_service._sync_access_token()
        is_newly_ready = isinstance(auth_status, dict) and auth_status.get("status") == "ready"
        
        # Construct segments
        blocks = []
        recovery_instruction = ""

        if vault_state:
            # Detect which fields are missing to formulate a recovery prompt
            required = ["first_name", "last_name", "client_number", "client_type", "email"]
            missing = [f for f in required if not vault_state.get(f)]
            
            if missing:
                recovery_instruction = (
                    "### RECOVERY MODE: PARTIAL DATA DETECTED ###\n"
                    f"The user was previously registering a client. Known: {list(vault_state.keys())}. "
                    "In your first message, you MUST acknowledge this and ask: 'I see we have a partial registration for "
                    f"{vault_state.get('first_name', 'a client')}. Would you like to resume or start fresh?'"
                )
            
            blocks.append(f"CLIENT PROFILE:\n{json.dumps(vault_state, indent=2)}")

        if event_draft and active_workflow == "calendar" and any(v is not None for v in event_draft.values()):
            # Mask sensitive internal fields
            clean_draft = {k: v for k, v in event_draft.items() if not k.startswith("_")}
            # Additional check: only append if we have actual data (not just nulls/False)
            if any(v for v in clean_draft.values() if v is not None):
                blocks.append(f"PENDING CALENDAR EVENT:\n{json.dumps(clean_draft, indent=2)}")
                
                if is_newly_ready:
                    recovery_instruction = (
                        "### OAUTH SUCCESS: RE-HYDRATION MODE ###\n"
                        "The user has JUST authorized their calendar. You have 'Legal Bugs' (or the draft title) ready to finalize. "
                        "In your first message, say: 'Great! I've confirmed your calendar access. Should I finalize the scheduling for "
                        f"\"{clean_draft.get('title', 'your meeting')}\" now?'"
                    )

        # 4. CONTACT DRAFT RE-HYDRATION
        contact_draft = metadata.get("contact_draft", {})
        if contact_draft and active_workflow == "contact" and any(v is not None for v in contact_draft.values()):
            clean_contact = {k: v for k, v in contact_draft.items() if v is not None}
            if clean_contact:
                blocks.append(f"PENDING CONTACT RECORD:\n{json.dumps(clean_contact, indent=2)}")
                
                # If we don't already have a recovery instruction (e.g. from Client Draft), add one
                if not recovery_instruction:
                    required_contact = ["first_name", "last_name", "email"]
                    missing_contact = [f.replace('_', ' ').title() for f in required_contact if not clean_contact.get(f)]
                    if missing_contact:
                        recovery_instruction = (
                            "### RECOVERY MODE: CONTACT INTAKE DETECTED ###\n"
                            f"The user was previously creating a contact. Known: {list(clean_contact.keys())}. "
                            f"Acknowledge the partial info and ask for the {missing_contact[0]}."
                        )

        # Return the structured block
        if not blocks:
            return None

        content = "\n\n".join(blocks)
        return {
            "injection": (
                f"\n\n### DATABASE VAULT (RECOVERED STATE)\n"
                f"The following data is ALREADY SYNCED. Use it to proceed:\n"
                f"```json\n{content}\n```\n"
                f"{recovery_instruction}"
            ),
            "recovery_instruction": recovery_instruction,
            "has_data": True
        }
    except Exception as e:
        logger.error(f"[REHYDRATION-ERROR] {e}")
        return None

def get_starter_chips():
    """Returns suggested actions for a blank state chat."""
    return [
        {"label": "📅 Schedule Consultation", "prompt": "I want to schedule a new consultation"},
        {"label": "👤 Register New Client", "prompt": "I want to register a new client"},
        {"label": "📊 View Recent Matters", "prompt": "What are my recent matters?"},
        {"label": "🔍 Look up Protocol", "prompt": "How do I process a client intake?"}
    ]

def format_sync_chat_payload(tenant_id, client_args=None, event_draft=None, history=None, active_workflow=None, thread_id=None, session_lifecycle="active", metadata=None):
    """
    Unified transformer for the Node.js 'chatsessions' model.
    Maps client fields to top-level columns and events/states to 'metadata'.
    """
    client_data = client_args or {}
    
    # Base metadata structure
    sys_metadata = {
        "chat_history": history if history is not None else [],
        "event_draft": event_draft if event_draft is not None else {},
        "active_workflow": active_workflow, 
        "session_lifecycle": session_lifecycle
    }
    
    # If a full metadata dict is provided (from the agent), use it as the base and merge sys fields
    if metadata:
        final_metadata = metadata.copy()
        # Ensure we don't accidentally wipe history if it was passed separately
        if history is not None: final_metadata["chat_history"] = history
        if event_draft is not None: final_metadata["event_draft"] = event_draft
        if active_workflow: final_metadata["active_workflow"] = active_workflow
        if session_lifecycle: final_metadata["session_lifecycle"] = session_lifecycle
    else:
        final_metadata = sys_metadata
    
    payload = {
        "tenantId": tenant_id,
        "threadId": thread_id,
        "first_name": client_data.get("first_name"),
        "last_name": client_data.get("last_name"),
        "client_number": client_data.get("client_number"),
        "client_type": client_data.get("client_type"),
        "email": client_data.get("email"),
        "metadata": final_metadata
    }
    return payload

