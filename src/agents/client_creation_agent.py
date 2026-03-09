# src/agents/client_creation_agent.py
import json
from src.logger import logger
from src.utils import format_sync_chat_payload

# The full list of fields required for a complete client record - ORDERED BY PRIORITY
REQUIRED_FIELDS = ["first_name", "last_name", "client_number", "client_type", "email"]

async def handle_client_creation(func_name, args, services, tenant_id, history):
    """
    Handles all logic related to client record creation and sequential conversation intake.
    """
    logger.info(f"[{tenant_id}] Handling Client Creation: {func_name}")

    # 1. FETCH FROM DATABASE (Session Recovery)
    db_data = {}
    db_metadata = {}
    try:
        resp = await services['calendar'].get_client_session(tenant_id)
        db_data = resp if isinstance(resp, dict) else (resp.json() if hasattr(resp, 'json') else {})
        db_metadata = db_data.get("metadata", {})
        
        # Recover chat history from metadata if available
        db_history = db_metadata.get("chat_history", [])
    except Exception as e:
        logger.error(f"[DB-RECOVERY] Failed to fetch session: {e}")
        db_history = []

    # 2. INITIALIZE & SAFE MERGE (Prioritize new args, fallback to DB)
    final_args = {
        "first_name": args.get("first_name") or db_data.get("first_name"),
        "last_name": args.get("last_name") or db_data.get("last_name"),
        "client_number": args.get("client_number") or db_data.get("client_number"),
        "client_type": args.get("client_type") or db_data.get("client_type"),
        "email": args.get("email") or db_data.get("email")          
    }

    # 3. SYNC TO DATABASE (Incremental Persistence)
    try:
        # Debug: Log what we are trying to save
        logger.info(f"[DB-SYNC] Prepared Args: {final_args}")
        
        # Use the unified payload formatter
        sync_payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=final_args,
            event_draft=db_metadata.get("event_draft"),
            history=history if history else db_history,
            active_workflow="client"
        )
        
        await services['calendar'].sync_client_session(sync_payload)
        logger.info(f"[DB-SYNC] Success for tenant {tenant_id}. Metadata keys: {list(sync_payload['metadata'].keys())}")
    except Exception as e:
        logger.error(f"[DB-SYNC] Failed to sync session: {e}", exc_info=True)

    # 5. CHECK FOR COMPLETION
    missing = [f for f in REQUIRED_FIELDS if not final_args.get(f)]

    if not missing:
        # ALL FIELDS CAPTURED: Finalize the record
        try:
            save_result = await services['calendar'].save_new_client(final_args, tenant_id)
            logger.info(f"Final record save result: {save_result}")
            
            # CLEAR DRAFT SESSION: Important to prevent the AI from seeing "Locked" data on the next new client
            try:
                wipe_payload = format_sync_chat_payload(
                    tenant_id=tenant_id,
                    client_args={
                        "first_name": None,
                        "last_name": None,
                        "client_number": None,
                        "client_type": None,
                        "email": None
                    },
                    event_draft={
                        "title": None, 
                        "startTime": None,
                        "summary": None,
                        "optional_fields_requested": False
                    },
                    active_workflow="cleared", 
                    history=history
                )
                await services['calendar'].sync_client_session(wipe_payload)
            except Exception as e:
                logger.error(f"[CLIENT] Sync wipe failed: {e}")

            await services['calendar'].clear_client_session(tenant_id)

            # Format the success message with a structured Markdown table for HTML rendering
            summary_table = (
                "### FINAL SUMMARY: CLIENT REGISTERED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"| **First Name** | {final_args.get('first_name')} |\n"
                f"| **Last Name** | {final_args.get('last_name')} |\n"
                f"| **ID Number** | {final_args.get('client_number', 'N/A')} |\n"
                f"| **Type** | {final_args.get('client_type', 'N/A')} |\n"
                f"| **Email** | {final_args.get('email', 'N/A')} |\n"
            )

            return {
                "status": "success",
                "message": f"SUCCESS! Here is the confirmation table:\n{summary_table}\n\n[SYSTEM INSTRUCTION]: You MUST output the exact Markdown table above to the user verbatim. Do not omit the table.",
                "data": final_args,
                "instructions": "After outputting the exact summary table, ask if they want to schedule an appointment or create a matter."
            }
        except Exception as e:
            logger.error(f"Final save failed: {e}")
            return {"status": "error", "message": "The system encountered an error while saving the final record."}

    else:
        # PARTIAL PROGRESS: Lock progress and instruct the AI on exactly what to ask next
        missing_str = ", ".join(missing)
        logger.warning(f"[INTAKE-PROGRESS] Captured: {', '.join([k for k,v in final_args.items() if v])} | Missing: {missing_str}")

        return {
            "status": "partial_success",
            "current_state": final_args,
            "message": f"Progress saved. Required: {missing_str}.",
            "response_instruction": f"CRITICAL: Data is missing. Do not confirm the save. Ask for {missing[0]} immediately."
        }
