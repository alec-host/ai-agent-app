# src/agent_manager.py
import json
from datetime import datetime
from .logger import logger

# Import Specialized Agents
from .agents.calendar_agent import handle_calendar
from .agents.rag_agent import handle_rag_lookup
from .agents.core_agent import handle_core_ops, get_workflow_recovery as core_recovery

async def get_rehydration_context(tenant_id, services):
    """
    Dispatcher-level rehydration aggregator.
    Calls each agent's hook to see if they have a recovery context.
    """
    try:
        calendar_service = services.get("calendar")
        if not calendar_service:
            return None

        db_session = await calendar_service.get_client_session(tenant_id)
        raw_metadata = db_session.get("metadata", {})
        
        if isinstance(raw_metadata, str):
            try:
                metadata = json.loads(raw_metadata)
            except:
                metadata = {}
        else:
            metadata = raw_metadata or {}

        lifecycle = metadata.get("session_lifecycle", "active")
        if lifecycle == "completed":
            return None

        # 1. AGENT REGISTRY FOR RECOVERY
        blocks = []
        from .agents.calendar_agent import get_workflow_recovery as cal_recovery
        from .agents.memory_agent import get_memory_recovery as mem_recovery
        # core_recovery is already imported above

        agent_hooks = [cal_recovery, core_recovery, mem_recovery]
        
        for hook in agent_hooks:
            recovery = hook(metadata, db_session)
            if not recovery:
                continue
                
            # Handle list of blocks (like Memory Agent) or single block
            items = recovery if isinstance(recovery, list) else [recovery]
            
            for item in items:
                block_content = f"{item['header']}\n{json.dumps(item['data'], indent=2)}"
                if item.get("instruction"):
                    block_content += f"\n\n{item['instruction']}"
                blocks.append(block_content)

        if not blocks:
            return None

        return {
            "injection": (
                "### DATABASE VAULT (CURRENT SESSION STATE) ###\n"
                "The following details are already captured. Use them to prevent redundant questions.\n\n"
                + "\n\n".join(blocks)
            )
        }

    except Exception as e:
        logger.error(f"[REHYDRATION-AGGREGATOR] Failed for {tenant_id}: {e}", exc_info=True)
        return None


async def execute_tool_call(tool_call, services, user_role, tenant_id, history, user_email: str = None, user_tz: str = None):
    """
    Acts as a central Dispatcher (Router).
    Injects shared context and routes tool calls to specialized agent handlers.
    """
    def _redact_dict(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, (dict, list)):
                    _redact_dict(v)
                elif isinstance(v, str):
                    new_val = v
                    if user_email and user_email in new_val: new_val = new_val.replace(user_email, "[REDACTED]")
                    if tenant_id and tenant_id in new_val: new_val = new_val.replace(tenant_id, "[REDACTED]")
                    obj[k] = new_val
        elif isinstance(obj, list):
            for i in range(len(obj)):
                val = obj[i]
                if isinstance(val, (dict, list)):
                    _redact_dict(val)
                elif isinstance(val, str):
                    new_val = val
                    if user_email and user_email in new_val: new_val = new_val.replace(user_email, "[REDACTED]")
                    if tenant_id and tenant_id in new_val: new_val = new_val.replace(tenant_id, "[REDACTED]")
                    obj[i] = new_val
        return obj

    func_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)

    # 1. INJECT TEMPORAL CONTEXT (Available to all agents)
    try:
        now_obj = datetime.now().astimezone()
        offset = now_obj.strftime("%z")
        args["_system_context"] = {
            "current_time": now_obj.isoformat(),
            "day_of_week": now_obj.strftime("%A"),
            "timezone_offset": f"{offset[0:3]}:{offset[3:5]}" if len(offset) == 5 else offset,
            "user_timezone_name": user_tz
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
    core_funcs = ["authenticate_to_core", "create_contact", "lookup_countries", "create_standard_event", "create_all_day_event", "lookup_client", "lookup_practice_area", "lookup_case_stage", "lookup_billing_type", "create_matter"]

    # --- WORKFLOW GATING (PREVENT OVERLAP) ---
    try:
        db_session = await services['calendar'].get_client_session(tenant_id)
        raw_metadata = db_session.get("metadata", {})
        
        # Robust parsing for string-encoded metadata
        if isinstance(raw_metadata, str):
            try:
                metadata = json.loads(raw_metadata)
            except:
                metadata = {}
        else:
            metadata = raw_metadata or {}

        lifecycle = metadata.get("session_lifecycle", "active")
        # Treat 'cleared' active_workflow AND 'completed' lifecycle as both meaning "no active lock"
        is_session_done = lifecycle == "completed" or metadata.get("active_workflow") in ["cleared", None]
        active_workflow = metadata.get("active_workflow") if not is_session_done else None
        # Normalize internal 'calendar' mode to 'google_calendar' for logic checks
        if active_workflow == "calendar": active_workflow = "google_calendar"
        
        # Define strict demarcation
        is_calendar_tool = func_name in calendar_funcs
        is_client_tool = func_name in client_funcs
        
        # 3. ROUTE TO SPECIALIST WITH GATING
        if is_calendar_tool:
            # GATING: If we are in ANY MatterMiner intake (Client, Contact, or MM-Event), block Google Calendar
            mm_intakes = ["client", "contact", "standard_event", "all_day_event"]
            if active_workflow in mm_intakes and func_name in ["schedule_event", "initialize_calendar_session"]:
                 return {"status": "error", "message": f"Conflict: Active {active_workflow} intake. Finish or cancel current MatterMiner workflow before using Google Calendar."}
            
            result = await handle_calendar(func_name, args, services['calendar'], user_role, history=history)
            
            # Post-execution Hook: If a new token was recovered, set it in the service
            if func_name == "initialize_calendar_session" and isinstance(result, dict) and result.get("status") == "ready":
                token = result.pop("jwtToken", None) # STRENGHTENED: Remove from result to prevent history leakage
                if token: services['calendar'].set_auth_token(token, is_jwt=True)
                result["_continue_chaining"] = True
            return _redact_dict(result)
            
        elif is_client_tool:
            # GATING: If we are in an active Google Calendar workflow, block MM-Client tools.
            if active_workflow == "google_calendar" and not is_session_done:
                return _redact_dict({"status": "error", "message": "Conflict: Active Google Calendar draft. Finish the event before starting a client registration."})

            return _redact_dict(await handle_core_ops(func_name, args, services, tenant_id, history, user_email=user_email))
            
        elif func_name in core_funcs:
            # GATING: If we are in an active Google Calendar workflow, block MM-Core tools (Contact/Event).
            if active_workflow == "google_calendar" and not is_session_done:
                 return _redact_dict({"status": "error", "message": "Conflict: Active Google Calendar draft. Finish the event before starting this MatterMiner operation."})

            return _redact_dict(await handle_core_ops(func_name, args, services, tenant_id, history, user_email=user_email))
            
        elif func_name in rag_funcs:
            return _redact_dict(await handle_rag_lookup(func_name, args, services, tenant_id))

    except Exception as e:
        logger.error(f"CRITICAL DISPATCH ERROR: {func_name} failed. {e}", exc_info=True)
        return _redact_dict({"status": "error", "message": "An internal error occurred in the tool dispatcher.", "details": str(e)})

    # 4. DEFAULT
    return _redact_dict({"status": "error", "message": f"Tool '{func_name}' is not recognized or has no registered handler.", "code": 404})
