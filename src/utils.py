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
            return ""

        resp = await calendar_service.request("GET", f"/chat/session?tenantId={tenant_id}")
        if not resp or not isinstance(resp, dict):
            return ""

        # 1. CLIENT VAULT
        vault_state = {k: v for k, v in {
            "client_number": resp.get("client_number"),
            "client_type": resp.get("client_type"),
            "first_name": resp.get("first_name"),
            "last_name": resp.get("last_name"), 
            "email": resp.get("email")
        }.items() if v}

        # 2. EVENT DRAFT (from metadata)
        metadata = resp.get("metadata", {})
        event_draft = metadata.get("event_draft", {})

        if not vault_state and not event_draft:
            return ""

        # Construct segments
        blocks = []
        if vault_state:
            blocks.append(f"CLIENT PROFILE:\n{json.dumps(vault_state, indent=2)}")
        if event_draft:
            # Mask sensitive internal fields
            clean_draft = {k: v for k, v in event_draft.items() if not k.startswith("_")}
            blocks.append(f"PENDING CALENDAR EVENT:\n{json.dumps(clean_draft, indent=2)}")

        # Return the structured block
        content = "\n\n".join(blocks)
        return (
            f"\n\n### DATABASE VAULT (RECOVERED STATE)\n"
            f"The following data is ALREADY SYNCED. Use it to proceed:\n"
            f"```json\n{content}\n```\n"
        )
    except Exception as e:
        logger.error(f"[REHYDRATION-ERROR] {e}")
        return ""

def format_sync_chat_payload(tenant_id, client_args=None, event_draft=None, history=None):
    """
    Unified transformer for the Node.js 'chatsessions' model.
    Maps client fields to top-level columns and events to 'metadata'.
    """
    client_data = client_args or {}
    
    # We maintain the existing schema while using 'metadata' for flexible storage
    metadata = {
        "chat_history": history if history else [],
        "event_draft": event_draft if event_draft else {}
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
