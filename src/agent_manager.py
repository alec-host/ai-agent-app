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

    rag_funcs = ["lookup_firm_protocol", "search_knowledge_base"]

    # --- 0. THE "HARD ANCHOR" (REPLACEMENT) ---
    last_data = {}
    is_mid_intake = False

    # 1. Search history for the last partial state
    for msg in reversed(history or []):
        role = msg.get("role") if isinstance(msg, dict) else getattr(msg, 'role', None)
        if role == "tool":
            content_raw = msg.get("content") if isinstance(msg, dict) else getattr(msg, 'content', "")
            try:
                content = json.loads(content_raw)
                # This looks for the 'partial_success' status we defined earlier
                if content.get("status") == "partial_success":
                    is_mid_intake = True
                    last_data = content.get("current_state", {})
                    break
            except: continue

    # 2. Force the tool and RE-INJECT the missing data
    if is_mid_intake and func_name not in ["create_client_record", "lookup_firm_protocol"]:
        logger.info(f"FORCE-REDIRECT: Re-anchoring to client intake for {last_data.get('full_name')}")
        func_name = "create_client_record"

        # This is the fix: It ensures the name 'Peter Pan' is passed 
        # into the NEW tool call even if the AI forgot to include it.
        for key, value in last_data.items():
            if not args.get(key):
                args[key] = value

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
        
    elif func_name in rag_funcs:
        try:
            logger.info(f"Agent executing RAG lookup: {func_name}")

            # We use the tenant_id passed into execute_tool_call for security
            user_query = args.get("query")

            if func_name == "lookup_firm_protocol":
                # This calls the method we added to your CalendarServiceClient
                result_context = services['calendar'].get_workflow_protocol(
                    query=user_query, 
                    tenant_id=tenant_id
                )
            return {"status": "success", "data": result_context}
        except Exception as e:
            logger.error(f"RAG lookup failed for {func_name}: {str(e)}")
            return {"error": "Knowledge base retrieval failed", "details": str(e)}

    client_funcs = ["create_client_record"]

    # helper in agent_manager.py
    def extract_missing_entities(text, current_args):
        if not text: return current_args

        # Simple regex for email if not already present
        if not current_args.get("email"):
            email_match = re.search(r'[\w\.-]+@[\w\.-]+\.\w+', text)
            if email_match:
                current_args["email"] = email_match.group(0)

        # If text is 2-3 words and title-cased, and name is missing, assume it's the name
        if not current_args.get("full_name"):
            words = text.strip().split()
            if 1 <= len(words) <= 3 and all(w[0].isupper() for w in words if w):
                current_args["full_name"] = text.strip()

        return current_args

    if func_name in client_funcs:
        required_fields = ["full_name", "email", "phone", "address"]

        # 1. First, harvest from previous TOOL CALLS (your existing logic)
        for msg in reversed(history or []):
            t_calls = msg.get("tool_calls") if isinstance(msg, dict) else getattr(msg, 'tool_calls', None)
            if t_calls:
                for tc in t_calls:
                    try:
                        tc_args_raw = tc.get("function", {}).get("arguments") if isinstance(tc, dict) else tc.function.arguments
                        tc_args = json.loads(tc_args_raw)
                        for field in required_fields:
                            if not args.get(field) and tc_args.get(field):
                                args[field] = tc_args[field]
                    except: continue

        # 2. NEW: Harvest from RAW TEXT (The "Peter Pan" Fix)
        # We look at the very last user message to see if they just typed a value
        if history:
            last_user_msg = next((m for m in reversed(history) if (m.get("role") if isinstance(m, dict) else m.role) == "user"), None)
            if last_user_msg:
                user_text = last_user_msg.get("content") if isinstance(last_user_msg, dict) else last_user_msg.content
                # Use our helper to find names/emails/phones in the plain text
                args = extract_missing_entities(user_text, args)

        # 3. Now check what is still missing
        missing = [f for f in required_fields if not args.get(f)]
        
        if not missing:
            try:
                result = await services['calendar'].save_new_client(args, tenant_id) 
                return {
                    "status": "success",
                    "message": f"RECORD COMPLETE: Client {args.get('full_name')} has been saved to the database.",
                    "data": args,
                    "instructions": "Inform the user the client is created and ask if they want to 'Create a Matter' for this client now."
                }
            except Exception as e:
                return { "error": "Final save failed", "details": str(e) }
        else:
            # Identify which fields we HAVE collected to inform the AI
            collected = {f: args.get(f) for f in required_fields if args.get(f)}
            #last_captured = collected[-1] if collected else "initial intent"
            return {
				"status": "partial_content",
                "current_state": args,
                "response_instruction": f"The record for '{args.get('full_name')}' is UPDATED. "
                                        f"STRICT: Do not ask for name again. "
                                        f"Your next output MUST be a question asking for the {missing[0]}."
            }

    return {"error": f"Tool {func_name} not recognized", "status": 404}
