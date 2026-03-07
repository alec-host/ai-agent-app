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
        
        # 3. SMART TRUNCATION
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

        resp = await calendar_service.request("GET", f"/chat/session?tenantId={tenant_id}")
        if not resp or not isinstance(resp, dict):
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
        
        auth_status = await calendar_service.request("GET", f"/auth/accessToken?tenant_id={tenant_id}")
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

        if event_draft:
            # Mask sensitive internal fields
            clean_draft = {k: v for k, v in event_draft.items() if not k.startswith("_")}
            blocks.append(f"PENDING CALENDAR EVENT:\n{json.dumps(clean_draft, indent=2)}")
            
            if is_newly_ready:
                recovery_instruction = (
                    "### OAUTH SUCCESS: RE-HYDRATION MODE ###\n"
                    "The user has JUST authorized their calendar. You have 'Legal Bugs' (or the draft title) ready to finalize. "
                    "In your first message, say: 'Great! I've confirmed your calendar access. Should I finalize the scheduling for "
                    f"\"{clean_draft.get('title', 'your meeting')}\" now?'"
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

def format_sync_chat_payload(tenant_id, client_args=None, event_draft=None, history=None, active_workflow=None):
    """
    Unified transformer for the Node.js 'chatsessions' model.
    Maps client fields to top-level columns and events/states to 'metadata'.
    """
    client_data = client_args or {}
    
    # We maintain the existing schema while using 'metadata' for flexible storage
    metadata = {
        "chat_history": history if history else [],
        "event_draft": event_draft if event_draft else {},
        "active_workflow": active_workflow # 'client' or 'calendar'
    }
    
    return {
        "tenantId": tenant_id,
        "first_name": client_data.get("first_name"),
        "last_name": client_data.get("last_name"),
        "client_number": client_data.get("client_number"),
        "client_type": client_data.get("client_type"),
        "email": client_data.get("email"),
        "metadata": metadata
    }
