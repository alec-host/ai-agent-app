import json
import re
# FIXED: Removed 'import datetime' to prevent name collision with the class import
from datetime import datetime, timedelta
from src.logger import logger
from src.agents.calendar_agent import handle_calendar
from src.agents.client_creation_agent import handle_client_intake_partial
from src.utils import format_sync_client_payload 

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
    if is_mid_intake:
        if func_name not in ["create_client_record", "lookup_firm_protocol"]:
            logger.info(f"FORCE-REDIRECT: Re-anchoring to client intake for {last_data.get('first name')}")
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
                result_context = await services['calendar'].get_workflow_protocol(
                    query=user_query, 
                    tenant_id=tenant_id
                )
            print(result_context)
            return {"status": "success", "data": result_context}
        except Exception as e:
            logger.error(f"RAG lookup failed for {func_name}: {str(e)}")
            return {"error": "Knowledge base retrieval failed", "details": str(e)}

    client_funcs = ["create_client_record", "setup_client"]


    if func_name in client_funcs:
        required_fields = ["first_name", "last_name", "client_number", "client_type", "email"]
        
        # 1. FETCH FROM DATABASE (Session Recovery)
        db_data = {}
        db_history = []
        try:
            resp = await services['calendar'].get_client_session(tenant_id)

            status_code = getattr(resp, 'status_code', resp.get('status') if isinstance(resp, dict) else None)

            if status_code == 200:
                if isinstance(resp, dict):
                    db_data = resp
                elif hasattr(resp, 'json'):
                    db_data = resp.json()
                else:
                    db_data = {}

                if db_data and (db_data.get('client_number') or db_data.get('email')):
                    # Recover chat history from metadata
                    db_history = db_data.get("metadata", {}).get("chat_history", [])

                    vault_parts = [f"{k}: {db_data.get(k)}" for k in required_fields if db_data.get(k)]
                    logger.info(f"[DEBUG-INTAKE] Vault Content: {', '.join(vault_parts)}")
                else:
                    logger.warning(f"[DB-RECOVERY] Response received but required client fields are missing: {db_data}")
            else:
                logger.warning(f"[DB-RECOVERY] Session not found or error status: {status_code}")
        except Exception as e:
            logger.error(f"[DB-RECOVERY] Failed to fetch session: {e}")

        sync_client_partial_payload = format_sync_payload(tenant_id, args, history)
        logger.info(f"[DB-SYNCTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTt]    {sync_client_partial_payload}")
        await services['calendar'].sync_client_session(sync_client_partial_payload)

        db_data = result.get("current_state", {})

        # 2. INITIALIZE & SAFE MERGE
        # We use 'or' to ensure that if 'args' has a null/empty value, 
        # we retain what was already in 'db_data'.
        final_args = {
            "first_name": args.get("first_name") or db_data.get("first_name"),
            "last_name": args.get("first_name") or db_data.get("last_name"),
            "client_number": args.get("client_number") or db_data.get("client_number"),
            "client_type": args.get("client_type") or db_data.get("client_type"),
            "email": args.get("email") or db_data.get("email")          
        }

        # 3. REHYDRATE HISTORY: If frontend history is empty [], use DB history
        db_history = db_data.get("metadata", {}).get("chat_history", [])
        effective_history = history if (history and len(history) > 0) else db_history

        # 4. SYNC TO DATABASE (Persistence)
        try:
            # Note: Ensure Node.js controller syncSession handles the 'history' key 
            # and saves it into metadata.chat_history
            sync_client_payload = format_sync_payload(tenant_id, final_args, effective_history)
            logger.info(f"[DB-SYNCTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTTt]    {sync_client_payload}")
            await services['calendar'].sync_client_session(sync_client_payload)
        except Exception as e:
            logger.error(f"[DB-SYNC] Failed to sync session: {e}")

        # 5. CHECK FOR COMPLETION
        missing = [f for f in required_fields if not final_args.get(f)]

        if not missing:
            try:
                # Save to permanent Client Database
                result = await services['calendar'].save_new_client(final_args, tenant_id)
                logger.info(f"Final save : {result}")
                
                # 6. CLEAR DB SESSION: Use the clear endpoint we created
                await services['calendar'].clear_client_session(tenant_id)

                return {
                    "status": "success",
                    "message": f"RECORD COMPLETE: {final_args.get('first_name')} has been saved.",
                    "data": final_args,
                    "instructions": "Inform the user the client record is created. Ask if they want to schedule an appointment or create a matter."
                }
            except Exception as e:
                logger.error(f"Final save failed: {e}")
                return {"status": "error", "message": "The system encountered an error while saving the final record."}

        else:
            # --- THE CIRCUIT BREAKER TRIGGERED ---
            # The AI tried to save, but we caught missing data
            # 7. PARTIAL SUCCESS: Lock progress and instruct the AI
            # Force it back to "partial_success" mode.
            missing_str = ", ".join(missing)
            logger.warning(f"[INTAKE-GUARD] AI tried to save but {missing_str} is missing.")

            return {
                "status": "partial_success",
                "current_state": final_args,
                "message": f"Cannot save yet. The following fields are still required: {missing_str}.",
                "response_instruction": f"CRITICAL: Data is missing. Do not confirm the save. Ask for {missing[0]} immediately."
            }

    return {"error": f"Tool {func_name} not recognized", "status": 404}
