import json
from datetime import datetime, timedelta
from src.logger import logger
from src.utils import format_sync_chat_payload

async def handle_calendar(func_name, args, calendar_service, user_role, history=None):
    """
    Specialist agent for all calendar operations.
    Handles temporal logic, auth chaining, and persistent 'Drafting' to prevent amnesia.
    """
    tenant_id = calendar_service.tenant_id
    logger.info(f"[{tenant_id}] Handling Calendar: {func_name}")
    
    # Extract temporal context injected by agent_manager
    sys_context = args.get("_system_context", {})
    ref_time = sys_context.get("current_time")
    
    # 1. FETCH CURRENT SESSION (For Drafting Persistence)
    db_data = {}
    try:
        resp = await calendar_service.get_client_session(tenant_id)
        db_data = resp if isinstance(resp, dict) else (resp.json() if hasattr(resp, 'json') else {})
    except: pass
    
    db_metadata = db_data.get("metadata", {})
    event_draft = db_metadata.get("event_draft", {})

    # --- 2. THE DRAFTING SYNC (Amnesia Fix) ---
    # Merge incoming args with the saved Draft
    if func_name in ["schedule_event", "update_event"]:
        current_draft = {
            "title": args.get("title") or args.get("summary") or event_draft.get("title"),
            "startTime": args.get("startTime") or event_draft.get("startTime"),
            "duration_minutes": args.get("duration_minutes") or event_draft.get("duration_minutes", 60)
        }
        
        # Immediate Persistence: Lock in what we know so far
        try:
            sync_payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=db_data, # Keep existing client data
                event_draft=current_draft,
                history=history,
                active_workflow="calendar"
            )
            await calendar_service.sync_client_session(sync_payload)
            logger.info(f"[CAL-DRAFT] Sync successful for '{current_draft.get('title')}'")
        except Exception as e:
            logger.error(f"[CAL-DRAFT] Sync failed: {e}")

        # --- 3. EXECUTION BLOCK ---
        if func_name == "schedule_event":
            # Validation
            if not current_draft.get("startTime"):
                # Progress sync: lock into 'calendar' workflow
                await calendar_service.sync_client_session(
                    format_sync_chat_payload(tenant_id, db_data, current_draft, history, active_workflow="calendar")
                )
                return {
                    "status": "partial_success",
                    "message": "Title captured. Need a start time.",
                    "response_instruction": "You have the title saved in the database. Ask the user for the specific date and time."
                }

            # Time Normalization
            duration = int(current_draft.get("duration_minutes", 60))
            try:
                end_time = calendar_service.calculate_end_time(current_draft["startTime"], duration, reference_time=ref_time)
                current_draft["endTime"] = end_time
            except Exception as e:
                return {"status": "error", "message": f"Invalid time format: {e}"}

            # PRE-FLIGHT SYNC (Last check) - includes workflow lock
            await calendar_service.sync_client_session(
                format_sync_chat_payload(tenant_id, db_data, current_draft, history, active_workflow="calendar")
            )

            # Execute save to actual calendar
            try:
                result = await calendar_service.request("POST", "/events", current_draft)
                
                # A: Auth Recovery Scenario
                if isinstance(result, dict) and result.get("status") == "auth_required":
                    return {
                        "status": "auth_required",
                        "auth_url": result.get("auth_url"),
                        "message": "Your Google session has expired. I have saved your meeting details.",
                        "response_instruction": "PROVIDE THE AUTH LINK to the user immediately. Reassure them the draft is safe."
                    }

                # B: Success Scenario
                if result.get("status") == "success" or "id" in result:
                    # SUCCESS: Perform the "Clean Exit"
                    # Wiping the session clears the 'active_workflow' and the draft
                    try:
                        wipe_payload = format_sync_chat_payload(
                            tenant_id=tenant_id,
                            client_args=db_data,
                            event_draft={},
                            active_workflow=None,
                            history=history
                        )
                        await calendar_service.sync_client_session(wipe_payload)
                    except Exception as e:
                        logger.error(f"[CAL] Sync wipe failed: {e}")
                    
                    await calendar_service.clear_client_session(tenant_id)
                    logger.info(f"[CAL] Event scheduled. Session cleared for tenant {tenant_id}")
                    
                    # Format the success message with a structured Markdown table for HTML rendering
                    summary_table = (
                        "### FINAL SUMMARY: EVENT SCHEDULED\n\n"
                        "| Detail | Information |\n"
                        "| :--- | :--- |\n"
                        f"| **Title** | {current_draft.get('title')} |\n"
                        f"| **Start Time** | {current_draft.get('startTime')} |\n"
                        f"| **End Time** | {current_draft.get('endTime', 'N/A')} |\n"
                        f"| **Meeting Type** | {current_draft.get('meetingType', 'Consultation')} |\n"
                    )

                    return {
                        "status": "success",
                        "message": f"SUCCESS! Here is the confirmation table:\n{summary_table}\n\n[SYSTEM INSTRUCTION]: You MUST output the exact Markdown table above to the user verbatim. Do not omit the table.",
                        "data": result
                    }
                
                return result
            except Exception as e:
                logger.error(f"Execution crash: {e}")
                return {"status": "error", "message": f"Calendar service failure: {e}"}

    # --- 4. SESSION INITIALIZATION ---
    if func_name == "initialize_calendar_session":
        # Lock into 'calendar' workflow even before any data is gathered
        await calendar_service.sync_client_session(
            format_sync_chat_payload(tenant_id, db_data, event_draft, history, active_workflow="calendar")
        )
        
        result = await calendar_service.request("GET", f"/auth/accessToken?tenant_id={tenant_id}")
        if isinstance(result, dict) and result.get("status") == "ready":
             result["message"] = "SUCCESS: Session ready. You are authorized to manage the calendar."
             result["_continue_chaining"] = True
        return result

    # --- 5. RETRIEVAL & DELETION ---
    if func_name == "get_all_events":
        return await calendar_service.request("GET", "/events")

    if func_name == "delete_event":
        if user_role != "admin": 
            return {"error": "Unauthorized: Admin role required for deletion."}
        return await calendar_service.request("DELETE", f"/events/{args.get('event_id')}")

    return {"error": f"Function {func_name} not implemented"}