import json
import re
# FIXED: Removed 'import datetime' to prevent name collision with the class import
from datetime import datetime, timedelta
from src.logger import logger
from src.agents.calendar_agent import handle_calendar

async def execute_tool_call(tool_call, services, user_role, tenant_id, history):
    """
    Processes tool calls with History Merging (Amnesia Fix), 
    Temporal Awareness, and JWT Token Chaining.
    """
    func_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    
    # --- 0. TENANT ENFORCEMENT ---
    if func_name == "initialize_calendar_session" and not args.get("tenant_id"):
        args["tenant_id"] = tenant_id

    # --- 1. THE "AMNESIA" FIX: HISTORY MERGING (ENHANCED) ---
    if func_name in ["schedule_event", "initialize_calendar_session"]:
        # First priority: Pull from previous tool call arguments
        for msg in reversed(history or []):
            t_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            if t_calls:
                for tc in t_calls:
                    try:
                        tc_args_raw = tc.get("function", {}).get("arguments") if isinstance(tc, dict) else tc.function.arguments
                        prev_args = json.loads(tc_args_raw)
                        if not args.get("startTime") and prev_args.get("startTime"):
                            args["startTime"] = prev_args["startTime"]
                        if not args.get("title") and (prev_args.get("title") or prev_args.get("summary")):
                            args["title"] = prev_args.get("title") or prev_args.get("summary")
                    except: continue

        # Second priority: If title is still missing, scan text content for specific mention
        if func_name == "schedule_event" and not args.get("title"):
             for msg in reversed(history or []):
                content = str(msg.get("content", "")) if isinstance(msg, dict) else getattr(msg, 'content', "")
                # Regex to find title mentioned in previous chat bubbles
                title_match = re.search(r"(?:title|subject)\s+(?:is|:)\s+['\"]?([^'\"\n]+)['\"]?", content, re.IGNORECASE)
                if title_match:
                    args["title"] = title_match.group(1)
                    break

    # --- 2. INJECT TEMPORAL CONTEXT ---
    try:
        now_obj = datetime.now().astimezone()
        offset = now_obj.strftime("%z")
        args["_system_context"] = {
            "current_time": now_obj.isoformat(),
            "day_of_week": now_obj.strftime("%A"),
            "timezone_offset": f"{offset[:3]}:{offset[3:]}"
        }
    except Exception as e:
        logger.error(f"Temporal injection failed: {str(e)}")

    calendar_funcs = [
        "get_all_events", "get_event_by_id", "schedule_event", 
        "delete_event", "update_event", "get_system_status", 
        "initialize_calendar_session", "check_calendar_connection"
    ]

    # --- 3. EXECUTE THE HANDLER ---
    if func_name in calendar_funcs:
        try:
            result = await handle_calendar(func_name, args, services['calendar'], user_role)
            if func_name == "initialize_calendar_session" and isinstance(result, dict) and result.get("status") == "ready":
                token = result.get("jwtToken")
                if token:
                    services['calendar'].set_auth_token(token)
                result["_continue_chaining"] = True
            return result
        except Exception as e:
            logger.error(f"Error in handle_calendar for {func_name}: {str(e)}")
            return {"error": "Internal handler error", "details": str(e)}

    return {"error": f"Tool {func_name} not recognized", "status": 404}