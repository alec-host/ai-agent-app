# src/utils.py

import json
import logging
import asyncio
import functools
from src.logger import logger

def sanitize_history(history: list, max_content_length: int = 2000, keep_last_n: int = 3, redact_values: list = None):
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
        
        if isinstance(content, str):
            # 3.4 GHOST DATA SCRUBBING
            # Completely strip out previous system injections from historical messages 
            # to prevent the LLM from hallucinating old state after a workflow completes.
            scrub_markers = [
                "### DATABASE VAULT",
                "### RECOVERY MODE",
                "### PENDING CONTACT",
                "### PENDING CALENDAR",
                "The following data is ALREADY SYNCED" # fallback
            ]
            for marker in scrub_markers:
                if marker in content:
                    content = content.split(marker)[0].strip()
            
            # 3.4.1 LITERAL VALUE REDACTION (High-Sensitivity Scrub)
            if redact_values:
                for val in redact_values:
                    if val and isinstance(val, str) and val in content:
                        content = content.replace(val, "[REDACTED]")
                    
            # 3.5 SECURITY & SENSITIVE TOKEN MASKING (Regex-based for Values)
            mask_targets = ["password", "jwtToken", "accessToken", "remote_access_token", "X-Tenant-ID", "Authorization", "X-User-Email"]
            for target in mask_targets:
                if target in content:
                    # Replace the key itelf
                    content = content.replace(target, "********")
                    # Also try to mask the value if it follows a JSON-like pattern: "key": "value"
                    # Pattern: \*\*\*\*\*\*\*\*"\s*:\s*"([^"]+)"
                    import re
                    content = re.sub(r'(\*\*\*\*\*\*\*\*"\s*:\s*")([^"]+)"', r'\1********"', content)
            
            # Additional keys to mask in generic objects
            msg_dict["content"] = content

            if not is_recent:
                if len(content) > max_content_length:
                    # Keep the beginning (summary/status) and cut the rest
                    msg_dict["content"] = content[:max_content_length] + f" ... [Truncated: {len(content) - max_content_length} chars]"
        
        # Mask passwords in tool_calls arguments if they exist
        if "tool_calls" in msg_dict and msg_dict["tool_calls"]:
            for tc in msg_dict["tool_calls"]:
                if tc.get("function") and tc["function"].get("arguments"):
                    try:
                        args_dict = json.loads(tc["function"]["arguments"])
                        mask_keys = ["password", "jwtToken", "accessToken", "remote_access_token", "X-Tenant-ID", "token", "X-User-Email", "user_email"]
                        for key in mask_keys:
                            if key in args_dict:
                                args_dict[key] = "********"
                        
                        # Literal redaction in arguments too
                        if redact_values:
                            args_str = json.dumps(args_dict)
                            for val in redact_values:
                                if val and isinstance(val, str) and val in args_str:
                                    args_str = args_str.replace(val, "[REDACTED]")
                            tc["function"]["arguments"] = args_str
                        else:
                            tc["function"]["arguments"] = json.dumps(args_dict)
                    except:
                        pass
        
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

def get_starter_chips(vault_metadata: dict = None):
    """Returns suggested actions for a blank state chat, prioritized by active drafts."""
    chips = []
    
    # 1. Check for DRAFTS in the vault to enable Proactive Resumption (Phase D)
    if vault_metadata:
        if vault_metadata.get("contact_draft"):
            chips.append({"label": "🔄 Resume Contact", "prompt": "Resume my contact creation"})
        if vault_metadata.get("client_draft"):
            chips.append({"label": "🔄 Resume Client", "prompt": "Resume my client registration"})
        if vault_metadata.get("event_draft"):
            chips.append({"label": "🔄 Resume Event", "prompt": "Resume my meeting draft"})
        if vault_metadata.get("matter_draft"):
            chips.append({"label": "🔄 Resume Matter", "prompt": "Resume my matter creation"})

    # 2. Default standard workflows
    chips.extend([
        {"label": "👤 Create Contact", "prompt": "I want to create a new contact"},
        {"label": "🏢 Register Client", "prompt": "I want to register a new client"},
        {"label": "⚖️ Create Matter", "prompt": "I want to create a new matter for an existing client"}
    ])
    
    # 3. Optimization: Limit to top 4 most relevant chips
    return chips[:4]

_SENTINEL = object()

def format_sync_chat_payload(tenant_id, client_args=None, event_draft=None, contact_draft=None, history=None, active_workflow=_SENTINEL, thread_id=None, session_lifecycle=_SENTINEL, metadata=None, client_draft=None, matter_draft=None):
    """
    Unified transformer for the Node.js 'chatsessions' model.
    Maps client fields to top-level columns and events/states to 'metadata'.
    
    STRICT SEPARATION:
    - metadata['client_draft']: For the 'Register New Client' workflow.
    - metadata['contact_draft']: For the 'Create Contact' workflow.
    - metadata['event_draft']: For the 'Calendar' workflow.
    - metadata['matter_draft']: For the 'Create Matter' workflow.
    """
    client_data = client_args or {}
    
    # 1. Start with the existing metadata as the base (Additive Sync)
    final_metadata = (metadata.copy() if metadata else {}).copy()
    
    # 2. Update namespaces if explicitly provided
    if history is not None:
        final_metadata["chat_history"] = history
    
    if event_draft is not None:
        final_metadata["event_draft"] = event_draft
        
    if contact_draft is not None:
        # GUARD: contact_draft must always be a dict, never a list of messages
        if isinstance(contact_draft, list):
            logger.warning("[PAYLOAD-GUARD] contact_draft was a list (corrupt). Wiping to {}")
            contact_draft = {}
        final_metadata["contact_draft"] = contact_draft

    if client_draft is not None:
        # GUARD: client_draft must always be a dict, never a list of messages
        if isinstance(client_draft, list):
            logger.warning("[PAYLOAD-GUARD] client_draft was a list (corrupt). Wiping to {}")
            client_draft = {}
        final_metadata["client_draft"] = client_draft

    if matter_draft is not None:
        # GUARD: matter_draft must always be a dict
        if isinstance(matter_draft, list):
            logger.warning("[PAYLOAD-GUARD] matter_draft was a list (corrupt). Wiping to {}")
            matter_draft = {}
        final_metadata["matter_draft"] = matter_draft
        
    if active_workflow is not _SENTINEL:
        final_metadata["active_workflow"] = active_workflow
        
    if session_lifecycle is not _SENTINEL:
        final_metadata["session_lifecycle"] = session_lifecycle
    
    # 3. Construct the flat payload for the database
    # Top-level columns are treated as the 'Identity' of the row.
    # Mirror first from client_draft, then fallback to contact_draft, then client_data.
    draft_email = (client_draft.get("client_email") or client_draft.get("email") if client_draft else None)
    if not draft_email and contact_draft:
        draft_email = contact_draft.get("client_email") or contact_draft.get("email")
    
    payload = {
        "tenantId": tenant_id,
        "threadId": thread_id,
        "first_name": (client_draft.get("first_name") if client_draft else None) or client_data.get("first_name"),
        "last_name": (client_draft.get("last_name") if client_draft else None) or client_data.get("last_name"),
        "client_number": (client_draft.get("client_number") if client_draft else None) or client_data.get("client_number"),
        "client_type": (client_draft.get("client_type") if client_draft else None) or client_data.get("client_type"),
        "email": draft_email or client_data.get("email"),
        "metadata": final_metadata
    }
    return payload

def standardize_response(payload: dict, history: list = None) -> dict:
    """
    Standardizes the server response by ensuring 'response' and 'history' attributes 
    are always present through injection if they are missing from the original payload.
    """
    # 1. Ensure 'response' exists (fallback to 'message' then 'content')
    if "response" not in payload:
        payload["response"] = payload.get("message") or payload.get("content") or ""
        
    # 2. Ensure 'history' exists (fallback to provided history or empty list)
    if "history" not in payload:
        payload["history"] = history or []
        
    return payload

