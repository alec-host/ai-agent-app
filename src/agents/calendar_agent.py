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

    # 0. PRE-FLIGHT AUTH CHECK (Enforced Gatekeeper)
    # Perform a LIGHTWEIGHT REAL CHECK to trigger silent healing in main.py if needed.
    if func_name in ["schedule_event", "get_all_events", "delete_event", "update_event"]:
        auth_status = await calendar_service.request("GET", "/events?maxResults=1")
        if isinstance(auth_status, dict) and auth_status.get("status") == "auth_required":
            auth_status["response_instruction"] = "Present only the auth link and ask the user to let you know once they have authorized access. Stop all other activities."
            return auth_status
    
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
            "title": args.get("title") or event_draft.get("title"),
            "startTime": args.get("startTime") or event_draft.get("startTime"),
            "duration_minutes": args.get("duration_minutes") or event_draft.get("duration_minutes", 60),
            "summary": args.get("description") or event_draft.get("summary"),
            "location": args.get("location") or event_draft.get("location"),
            "attendees": args.get("attendees") or event_draft.get("attendees", []),
            "summary_requested": event_draft.get("summary_requested", False),
            "attendees_requested": event_draft.get("attendees_requested", False),
            "location_requested": event_draft.get("location_requested", False)
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
            if not current_draft.get("startTime") or not current_draft.get("title"):
                # Progress sync: lock into 'calendar' workflow
                await calendar_service.sync_client_session(
                    format_sync_chat_payload(tenant_id, db_data, current_draft, history, active_workflow="calendar")
                )
                missing = []
                if not current_draft.get("title"): missing.append("an Event Title")
                if not current_draft.get("startTime"): missing.append("a specific Date and Time")
                
                return {
                    "status": "partial_success",
                    "message": f"Captured partial details. Still need: {', '.join(missing)}.",
                    "response_instruction": f"You have some details saved, but you are missing {', '.join(missing)}. Explicitly ask the user for them. DO NOT ASSUME DEFAULTS like 'Consultation'."
                }

            # --- Step-by-Step Conversational Workflow ---
            # Instead of asking for all optional fields at once, we request them one-by-one.
            if not current_draft.get("summary_requested"):
                current_draft["summary_requested"] = True
                await calendar_service.sync_client_session(
                    format_sync_chat_payload(tenant_id, db_data, current_draft, history, active_workflow="calendar")
                )
                return {
                    "status": "partial_success",
                    "message": "Title and Time captured. Moving to step 1 (Summary).",
                    "response_instruction": "You have the Event Title and Start Time saved. Now, ask the user ONLY for a brief meeting summary or description. Do NOT finalize the booking yet."
                }

            if not current_draft.get("attendees_requested"):
                current_draft["attendees_requested"] = True
                await calendar_service.sync_client_session(
                    format_sync_chat_payload(tenant_id, db_data, current_draft, history, active_workflow="calendar")
                )
                return {
                    "status": "partial_success",
                    "message": "Summary step handled. Moving to step 2 (Attendees).",
                    "response_instruction": "Meeting summary handled. Now, ask the user ONLY for any attendees' emails they would like to add. If none, they can say 'none' or 'skip'."
                }

            if not current_draft.get("location_requested"):
                current_draft["location_requested"] = True
                await calendar_service.sync_client_session(
                    format_sync_chat_payload(tenant_id, db_data, current_draft, history, active_workflow="calendar")
                )
                return {
                    "status": "partial_success",
                    "message": "Attendees step handled. Moving to step 3 (Location).",
                    "response_instruction": "Attendees handled. Now, ask the user ONLY for a meeting location or venue. This is the last optional step."
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
                            event_draft={
                                "title": None, 
                                "startTime": None,
                                "summary": None,
                                "location": None,
                                "attendees": [],
                                "summary_requested": False,
                                "attendees_requested": False,
                                "location_requested": False
                            },
                            active_workflow=None, # Explicitly nullify the workflow
                            history=history
                        )
                        await calendar_service.sync_client_session(wipe_payload)
                    except Exception as e:
                        logger.error(f"[CAL] Sync wipe failed: {e}")
                    
                    await calendar_service.clear_client_session(tenant_id)
                    logger.info(f"[CAL] Event scheduled. Session cleared for tenant {tenant_id}")
                    
                    # Format the success message with a structured Markdown table for HTML rendering
                    attendees_list = current_draft.get('attendees', [])
                    attendees_str = ", ".join(attendees_list) if attendees_list else "None"
                    
                    summary_table = (
                        "| Detail | Information |\n"
                        "| :--- | :--- |\n"
                        f"| **Title** | {current_draft.get('title')} |\n"
                        f"| **Start Time** | {current_draft.get('startTime')} |\n"
                        f"| **End Time** | {current_draft.get('endTime', 'N/A')} |\n"
                        f"| **Summary** | {current_draft.get('summary', 'None')} |\n"
                        f"| **Location** | {current_draft.get('location', 'None')} |\n"
                        f"| **Attendees** | {attendees_str} |\n"
                    )

                    return {
                        "status": "success",
                        "message": f"### ✅ EVENT SCHEDULED SUCCESSFULLY\n\n{summary_table}\n\n**The session has been cleared.**",
                        "data": result,
                        "_exit_loop": True
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
        
        # PROACTIVE AUTH HEAL: Perform a 'Real' lightweight calendar operation.
        # This will trigger the Silent healing in `main.py` if the token is expired but refreshable.
        result = await calendar_service.request("GET", "/events?maxResults=1")
        if isinstance(result, dict):
            # If successfully retrieved (means token is fresh/healed)
            if result.get("status") == "success" or "items" in result:
                return {
                    "status": "ready",
                    "message": "SUCCESS: Calendar access is verified and ready.",
                    "_continue_chaining": True
                }
            # If it returns auth_required from the healing interceptor
            elif result.get("status") == "auth_required":
                result["response_instruction"] = "Your Google Calendar is not authorized. Present ONLY the link below and ask them to let you know when done. STOP EVERYTHING ELSE."
                return result

        # Fallback to status check if GET /events was inconclusive
        check = await calendar_service.request("GET", f"/auth/accessToken?tenant_id={tenant_id}")
        return check

    # --- 5. RETRIEVAL & DELETION ---
    if func_name == "get_all_events":
        return await calendar_service.request("GET", "/events")

    if func_name == "delete_event":
        if user_role != "admin": 
            return {"error": "Unauthorized: Admin role required for deletion."}
        return await calendar_service.request("DELETE", f"/events/{args.get('event_id')}")

    return {"error": f"Function {func_name} not implemented"}