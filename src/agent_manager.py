# src/agent_manager.py
import json
from datetime import datetime
from .logger import logger

# Import Specialized Agents
from .agents.calendar_agent import handle_calendar
from .agents.rag_agent import handle_rag_lookup
from .agents.core_agent import handle_core_ops, get_workflow_recovery as core_recovery

async def get_rehydration_context(tenant_id, services, user_email=None):
    """
    Dispatcher-level rehydration aggregator.
    Calls each agent's hook to see if they have a recovery context.
    """
    try:
        calendar_service = services.get("calendar")
        if not calendar_service:
            return None

        db_session = await calendar_service.get_client_session(tenant_id, user_email=user_email)
        raw_metadata = db_session.get("metadata", {})
        
        # [PHASE A: ISOLATION CHECK]
        # Skip rehydration if the session belongs to a different user
        if isinstance(raw_metadata, str):
            try: meta_check = json.loads(raw_metadata)
            except: meta_check = {}
        else:
            meta_check = raw_metadata or {}
            
        owner = meta_check.get("owner_email")
        if owner and user_email and owner != user_email:
             logger.info(f"[{tenant_id}] Rehydration skipped: Session owned by {owner}, request by {user_email}")
             return None
        
        if isinstance(raw_metadata, str):
            try:
                metadata = json.loads(raw_metadata)
            except (json.JSONDecodeError, ValueError):
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
            ),
             "has_data": True,
             "metadata": metadata
        }
    except Exception as e:
        logger.error(f"Rehydration context retrieval failed: {e}", exc_info=True)
        return None


async def execute_tool_call(tool_call, services, user_role, tenant_id, history, user_email: str = None, user_tz: str = None, ai_client = None):
    """
    Acts as a central Dispatcher (Router).
    Injects shared context and routes tool calls to specialized agent handlers.
    """
    func_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments) if isinstance(tool_call.function.arguments, str) else tool_call.function.arguments

    # Shared Helper to redact sensitive values from tool response logs
    def _redact_dict(d: dict) -> dict:
        if not isinstance(d, dict): return d
        redact_keys = ["jwtToken", "accessToken", "password", "token"]
        res = {k: ("********" if k in redact_keys else v) for k, v in d.items()}
        
        # New: Literal redaction for PII values in strings (SEC-06 hardening)
        redact_vals = [user_email, tenant_id]
        for k, v in res.items():
            if isinstance(v, str):
                for val in redact_vals:
                    if val and val in v:
                        v = v.replace(val, "[REDACTED]")
                        res[k] = v
        return res

    calendar_funcs = ["schedule_event", "initialize_calendar_session", "check_calendar_connection", "list_upcoming_events"]
    rag_funcs = ["lookup_firm_protocol", "search_knowledge_base"]
    # Phase 3 (Auth Migration): authenticate_to_core REMOVED from routing table.
    core_funcs = [
        "create_contact", "lookup_countries", 
        "create_standard_event", "create_all_day_event", "lookup_client", 
        "lookup_practice_area", "lookup_case_stage", "lookup_billing_type", 
        "create_matter", "create_client_record", "setup_client", "promote_contact_to_client"
    ]
    memory_funcs = ["recall_past_conversation"]

    # --- WORKFLOW GATING (PREVENT OVERLAP) ---
    try:
        db_session = await services['calendar'].get_client_session(tenant_id)
        raw_metadata = db_session.get("metadata", {})
        if isinstance(raw_metadata, str):
            try: metadata = json.loads(raw_metadata)
            except (json.JSONDecodeError, ValueError): metadata = {}
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
        
        # 3. ROUTE TO SPECIALIST WITH GATING
        result = None
        if is_calendar_tool:
            # GATING: If we are in ANY MatterMiner intake (Client, Contact, or MM-Event), block Google Calendar
            mm_intakes = ["client", "contact", "standard_event", "all_day_event", "matter"]
            if active_workflow in mm_intakes and func_name in ["schedule_event", "initialize_calendar_session"]:
                 return {"status": "error", "message": f"Conflict: Active {active_workflow} intake. Finish or cancel current MatterMiner workflow before using Google Calendar."}
            
            result = await handle_calendar(func_name, args, services['calendar'], user_role, history=history)
            
            # Post-execution Hook: If a new token was recovered, set it in the service
            if func_name == "initialize_calendar_session" and isinstance(result, dict) and result.get("status") == "ready":
                token = result.pop("jwtToken", None) # STRENGHTENED: Remove from result to prevent history leakage
                if token: services['calendar'].set_auth_token(token, is_jwt=True)
        
        elif func_name in core_funcs:
            # --- GUARD 01: PRE-EXECUTION AUTH ENTROPY ---
            # Enforce strict sign-in verification before allowing any Core API interaction.
            # If user_email is missing or headers are invalid, halt and redirect to login.
            if not user_email:
                logger.warning(f"[{tenant_id}] BLOCKED: Tool '{func_name}' called by unauthenticated user.")
                return {
                    "status": "auth_required",
                    "auth_type": "matterminer_core",
                    "message": "Authentication required for MatterMiner Core.",
                    "response_instruction": "Halt the current conversation and display the login card. Do not attempt to proceed with the tool call."
                }

            # --- STRATEGY: UNIFIED CORE OPERATIONS ---
            # Passing full context (history + email) for isolated multi-turn drafting.
            # Tunneling db_session to prevent latency-inducing redundant fetch cycles.
            result = await handle_core_ops(func_name, args, services, tenant_id, history, user_email=user_email, db_session=db_session)

        elif func_name in rag_funcs:
            result = _redact_dict(await handle_rag_lookup(func_name, args, services, tenant_id))

        elif func_name in memory_funcs:
            from .agents.memory_agent import handle_recall
            result = _redact_dict(await handle_recall(func_name, args, tenant_id, metadata, db_session, ai_client))

        if result:
            return _redact_dict(result)

        # 4. DEFAULT
        return _redact_dict({"status": "error", "message": f"Tool '{func_name}' is not recognized or has no registered handler.", "code": 404})

    except Exception as e:
        logger.error(f"CRITICAL DISPATCH ERROR: {func_name} failed. {e}", exc_info=True)
        return _redact_dict({"status": "error", "message": "An internal error occurred in the tool dispatcher."})

    finally:
        # --- PHASE E: AUTOMATED STATE PURGING (Efficiency & Scalability) ---
        # Triggered only on terminal success status.
        try:
            if isinstance(result, dict) and result.get("status") == "success" and result.get("_exit_loop"):
                from .utils import format_sync_chat_payload
                
                # Payload to Nullify/Reset the session state
                purge_payload = {"active_workflow": None, "session_lifecycle": "completed"}
                
                # Direct draft targeting based on the specialist result
                if func_name == "create_contact": purge_payload["contact_draft"] = {}
                elif func_name == "create_client_record": purge_payload["client_draft"] = {}
                elif func_name == "create_matter": purge_payload["matter_draft"] = {}
                elif func_name in ["schedule_event", "create_standard_event"]: purge_payload["event_draft"] = {}
                
                clean_payload = format_sync_chat_payload(
                    tenant_id, 
                    metadata=metadata, 
                    # Set the specific draft to empty in addition to workflow reset
                    **purge_payload
                )
                await services['calendar'].sync_client_session(clean_payload)
                logger.info(f"[PURGE] Success. Cleaned up {func_name} state for tenant {tenant_id}")
        except Exception as cleanup_err:
             logger.warning(f"[PURGE-FAILED] Non-fatal state cleanup error: {cleanup_err}")
