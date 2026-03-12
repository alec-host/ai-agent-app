# src/agent_manager.py
import json
from datetime import datetime
from .logger import logger

# Import Specialized Agents
from .agents.calendar_agent import handle_calendar
from .agents.client_creation_agent import handle_client_creation
from .agents.rag_agent import handle_rag_lookup
from .agents.core_agent import handle_core_ops

async def execute_tool_call(tool_call, services, user_role, tenant_id, history):
    """
    Acts as a central Dispatcher (Router).
    Injects shared context and routes tool calls to specialized agent handlers.
    """
    func_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    # 1. INJECT TEMPORAL CONTEXT (Available to all agents)
    try:
        now_obj = datetime.now().astimezone()
        offset = now_obj.strftime("%z")
        args["_system_context"] = {
            "current_time": now_obj.isoformat(),
            "day_of_week": now_obj.strftime("%A"),
            "timezone_offset": f"{offset[0:3]}:{offset[3:5]}" if len(offset) == 5 else offset
        }
    except Exception as e:
        logger.error(f"Temporal injection failed: {str(e)}")

    # 2. DEFINE SERVICE ROUTING
    calendar_funcs = [
        "get_all_events", "get_event_by_id", "schedule_event", 
        "delete_event", "update_event", "get_system_status", 
        "initialize_calendar_session", "check_calendar_connection"
    ]
    client_funcs = ["create_client_record", "setup_client"]
    rag_funcs = ["lookup_firm_protocol", "search_knowledge_base"]
    core_funcs = ["authenticate_to_core", "create_contact", "lookup_countries"]

    # --- WORKFLOW GATING (PREVENT OVERLAP) ---
    try:
        db_session = await services['calendar'].get_client_session(tenant_id)
        metadata = db_session.get("metadata", {})
        active_workflow = metadata.get("active_workflow")  # 'calendar' or 'client'
        
        # Define strict demarcation
        is_calendar_tool = func_name in calendar_funcs
        is_client_tool = func_name in client_funcs
        
        # 3. ROUTE TO SPECIALIST WITH GATING
        if is_calendar_tool:
            # If we are strictly in Client mode, block calendar tools unless it's a retrieval tool
            if active_workflow == "client" and func_name in ["schedule_event", "initialize_calendar_session"]:
                 return {"status": "error", "message": "Conflict: Active client intake. Finish or cancel client registration first."}
            
            result = await handle_calendar(func_name, args, services['calendar'], user_role, history=history)
            
            # Post-execution Hook: If a new token was recovered, set it in the service
            if func_name == "initialize_calendar_session" and isinstance(result, dict) and result.get("status") == "ready":
                token = result.get("jwtToken")
                if token: services['calendar'].set_auth_token(token)
                result["_continue_chaining"] = True
            return result
            
        elif is_client_tool:
            # GATING: If we are in Calendar mode, block client tools.
            # This prevents meeting titles like "Legal Battles" from triggering client creation.
            if active_workflow == "calendar":
                return {"status": "error", "message": "Conflict: Active calendar draft. Finish the event before creating a client."}

            return await handle_client_creation(func_name, args, services, tenant_id, history)
            
        elif func_name in core_funcs:
            return await handle_core_ops(func_name, args, services, tenant_id, history)
            
        elif func_name in rag_funcs:
            return await handle_rag_lookup(func_name, args, services, tenant_id)

    except Exception as e:
        logger.error(f"CRITICAL DISPATCH ERROR: {func_name} failed. {e}", exc_info=True)
        return {"status": "error", "message": "An internal error occurred in the tool dispatcher.", "details": str(e)}

    # 4. DEFAULT
    return {"status": "error", "message": f"Tool '{func_name}' is not recognized or has no registered handler.", "code": 404}
