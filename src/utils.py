# src/utils.py

import json
import logging
import asyncio
import functools
from typing import Any, List, Dict, Union
from src.logger import logger

def sanitize_history(history: list, max_content_length: int = 2000, keep_last_n: int = 3, redact_values: list = None):
    """
    Truncates older message content to save tokens, but STRICTLY PRESERVES 
    the most recent messages to ensure immediate context and JSON validity.
    
    [SECURITY & INTEGRITY]
    1. Redacts sensitive info (JWTs, Passwords).
    2. Heals corrupted assistant-tool chains by dropping incomplete tool calls.
    """
    sanitized = []
    total_raw = len(history)

    # 1. First Pass: Conversion and Basic Cleanup
    for i, msg in enumerate(history):
        if hasattr(msg, 'model_dump'):
            msg_dict = msg.model_dump() # Note: No exclude_none=True to keep 'content' keys
        elif hasattr(msg, 'dict'):
            msg_dict = msg.dict()
        else:
            msg_dict = dict(msg)
        
        # Identity field preservation
        if msg_dict.get("role") == "tool":
            if msg_dict.get("content") is None: msg_dict["content"] = ""
        
        # Scrubbing & Truncation (Same as before)
        is_recent = i >= (total_raw - keep_last_n)
        content = msg_dict.get("content")
        
        if isinstance(content, str):
            scrub_markers = ["### DATABASE VAULT", "### RECOVERY MODE", "### PENDING CONTACT", "### PENDING CALENDAR"]
            for marker in scrub_markers:
                if marker in content: content = content.split(marker)[0].strip()
            
            if redact_values:
                for val in redact_values:
                    if val and isinstance(val, str) and val in content:
                        content = content.replace(val, "[REDACTED]")
            
            mask_targets = ["password", "jwtToken", "accessToken", "remote_access_token", "X-Tenant-ID", "Authorization", "X-User-Email"]
            for target in mask_targets:
                if target in content:
                    content = content.replace(target, "********")
                    import re
                    # Robust JSON-key-value masking (masks "key": "value" after key was turned into asterisks)
                    content = re.sub(r'("\*\*\*\*\*\*\*\*"\s*:\s*")([^"]+)"', r'\1********"', content)
                    content = re.sub(r'(\*\*\*\*\*\*\*\*"\s*:\s*")([^"]+)"', r'\1********"', content)
            
            msg_dict["content"] = content
            if not is_recent and content and len(content) > max_content_length:
                msg_dict["content"] = content[:max_content_length] + f" ... [Truncated]"

        # --- TOOL CALL ARGUMENT MASKING (Phase 6: Hardening) ---
        if msg_dict.get("tool_calls"):
            for tc in msg_dict["tool_calls"]:
                args_str = tc.get("function", {}).get("arguments")
                if args_str and isinstance(args_str, str):
                    mask_targets = ["password", "jwtToken", "accessToken", "remote_access_token", "X-Tenant-ID", "Authorization", "X-User-Email"]
                    for target in mask_targets:
                        if target in args_str:
                            args_str = args_str.replace(target, "********")
                            import re
                            args_str = re.sub(r'("\*\*\*\*\*\*\*\*"\s*:\s*")([^"]+)"', r'\1********"', args_str)
                            args_str = re.sub(r'(\*\*\*\*\*\*\*\*"\s*:\s*")([^"]+)"', r'\1********"', args_str)
                    tc["function"]["arguments"] = args_str
        
        sanitized.append(msg_dict)

    # 2. Second Pass: Logic Healing (Prevent 400 BadRequest)
    # We must ensure every Assistant tool_call is followed by its Tool results.
    healed = []
    for i, msg in enumerate(sanitized):
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            # Look ahead for tool results
            call_ids = [tc.get("id") for tc in msg.get("tool_calls")]
            # We check the next sequence of messages for 'tool' roles with matching IDs
            found_ids = set()
            j = i + 1
            while j < len(sanitized) and sanitized[j].get("role") == "tool":
                tid = sanitized[j].get("tool_call_id")
                if tid in call_ids:
                    found_ids.add(tid)
                j += 1
            
            if len(found_ids) == len(call_ids):
                # Chain is complete, keep it
                healed.append(msg)
            else:
                # Chain is broken (missing tool results). 
                # DROP the tool_calls part to satisfy OpenAI, keep content if any.
                msg_copy = {**msg}
                msg_copy.pop("tool_calls", None)
                if msg_copy.get("content"):
                    healed.append(msg_copy)
                else:
                    # If it has no content and no tool calls, it's an empty shell, drop it completely.
                    continue
        else:
            healed.append(msg)

    return healed
    
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

def compact_tool_result(result: Any, max_len: int = 1500) -> str:
    """
    Truncates extremely large tool responses to prevent token overflow in Redis/LLM.
    If the result is a massive list, we keep the first few items and summarize the rest.
    """
    if not result:
        return ""
        
    try:
        raw_str = json.dumps(result) if not isinstance(result, str) else result
        if len(raw_str) <= max_len:
            return raw_str
            
        # If it's a list, provide a summary
        if isinstance(result, list):
            summary = [result[0]] if len(result) > 0 else []
            return json.dumps({
                "summary": summary,
                "total_count": len(result),
                "notice": "Results truncated for token efficiency."
            })
            
        return raw_str[:max_len] + "... [Truncated]"
    except Exception:
        return str(result)[:max_len]

def compress_reasoning_history(history: List[Dict[str, Any]], keep_reasoning_turns: int = 2) -> List[Dict[str, Any]]:
    """
    Cost Optimization: Keeps the full conversation text, but drops granular 
    tool reasoning for older turns to save massive amounts of tokens.
    """
    if len(history) <= 10:
        return history
        
    compressed = []
    reasoning_chains_found = 0
    
    # Iterate backwards to keep the most recent
    for msg in reversed(history):
        role = msg.get("role")
        is_reasoning = "tool_calls" in msg or role == "tool"
        
        if is_reasoning:
            if reasoning_chains_found < keep_reasoning_turns:
                compressed.append(msg)
        else:
            compressed.append(msg)
            # A user message marks the completion of a reasoning cycle for the previous (since we are moving backwards) assistant response.
            if role == "user":
                reasoning_chains_found += 1
            
    return list(reversed(compressed))
