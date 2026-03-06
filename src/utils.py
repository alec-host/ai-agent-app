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
        # Access the service exactly how your main.py does
        calendar_service = services.get("calendar")
        if not calendar_service:
            return ""

        # Make the request to your Node.js endpoint
        resp = await calendar_service.request("GET", f"/chat/session?tenantId={tenant_id}")
        
        # Check if we actually got a valid session
        if not resp or not isinstance(resp, dict):
            return ""

        # Extract the key fields
        vault_state = {k: v for k, v in {
            "client_number": resp.get("client_number"),
            "client_type": resp.get("client_type"),
            "first_name": resp.get("first_name"),
            "last_name": resp.get("last_name"), 
            "email": resp.get("email")
        }.items() if v}

        if not vault_state:
            return ""

        # Determine the guidance for the AI
        all_required = ["client_number", "client_type", "first_name", "last_name", "email"]
        missing = [f for f in all_required if f not in vault_state]
        
        guidance = f"Ask for {missing[0]} next." if missing else "Intake complete. Proceed to next task."

        # Return the structured block
        return (
            f"\n\n### DATABASE VAULT (RECOVERED STATE)\n"
            f"The following client data is already saved. DO NOT ask for these:\n"
            f"```json\n{json.dumps(vault_state, indent=2)}\n```\n"
            f"TARGET: {guidance}\n"
        )
    except Exception as e:
        logger.error(f"[REHYDRATION-ERROR] {e}")
        return ""

def format_sync_client_payload(tenant_id, args, history):
    """
    Transforms merged AI args into the structure 
    the Node.js 'syncChatSession' controller expects.
    """
    # Ensure args is a dictionary to prevent .get() crashes
    data = args if args is not None else {}
    
    return {
        "tenantId": tenant_id,
        "first_name": data.get("first_name"),
        "last_name": data.get("last_name"),
        "client_number": data.get("client_number"),
        "client_type": data.get("client_type"),
        "email": data.get("email"),
        # Matches the 'chat_history' destructuring in Node.js
        "chat_history": history if history else []
    }
