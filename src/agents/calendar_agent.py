import json
# 1. FIXED IMPORTS: Use this exact pattern to prevent 'datetime.datetime' errors
from datetime import datetime, timedelta
from src.logger import logger

async def handle_calendar(func_name, args, calendar_service, user_role):
    logger.info(f"[{calendar_service.correlation_id}] Handling: {func_name}")
    
    # Extract temporal context injected by agent_manager
    sys_context = args.get("_system_context", {})
    ref_time = sys_context.get("current_time")
    
    # --- 1. SCHEDULE & UPDATE (Time-Sensitive) ---
    if func_name in ["schedule_event", "update_event"]:
        # Map summary to title for Node.js compatibility
        input_title = args.get("title") or args.get("summary")
        args["title"] = input_title if input_title else "Scheduled Meeting"
        # Remove 'summary' to keep args clean
        args.pop("summary", None)

        if args.get("startTime"):
            duration = int(args.get("duration_minutes", 60))
            
            # 2. SAFETY BLOCK: Prevent crash from breaking the API chain
            try:
                # Service now uses ref_time to resolve 'next 30 mins'
                req_end_str = calendar_service.calculate_end_time(
                    args["startTime"], duration, reference_time=ref_time
                )
                
                if not req_end_str:
                    # Return a clean error instead of crashing
                    return {"error": "Invalid time format provided. Please use ISO format or clear relative time."}
                
                args["endTime"] = req_end_str

            except Exception as e:
                # Catch the datetime error here so we can log it and return a valid JSON response
                # This prevents the "Tool Call without Tool Result" (400 Bad Request) error
                logger.error(f"Time calculation crash: {str(e)}")
                return {"error": f"Internal time calculation failed: {str(e)}"}

            # Optional: Conflict Check could go here
            # ...

        endpoint = "/events" if func_name == "schedule_event" else f"/events/{args.get('event_id')}"
        method = "POST" if func_name == "schedule_event" else "PATCH"
        
        # 3. SERVICE REQUEST
        try:
            return await calendar_service.request(method, endpoint, args)
        except Exception as e:
             logger.error(f"Service request failed: {str(e)}")
             return {"error": "Calendar service is currently unavailable."}

    # --- 2. SESSION INITIALIZATION ---
    if func_name == "initialize_calendar_session":
        # Note: tenant_id is pulled from args
        result = await calendar_service.request("GET", f"/auth/accessToken?tenant_id={args.get('tenant_id')}")
        
        if isinstance(result, dict) and result.get("status") == "ready":
            # Extract what the AI sent to "lock" it in
            p_title = args.get("title") or args.get("summary")
            p_start = args.get("startTime")
            
            # IMPROVEMENT: Only mention facts we actually have. 
            # This prevents the AI from seeing 'StartTime=None' and getting confused.
            found_facts = []
            if p_title: found_facts.append(f"Title='{p_title}'")
            if p_start: found_facts.append(f"StartTime='{p_start}'")
            
            fact_block = f" [FACTS RECOVERED: {', '.join(found_facts)}]" if found_facts else ""

            result["message"] = (
                f"SUCCESS: Session ready.{fact_block} "
                "INSTRUCTION: You are now authorized. Check the PENDING DATA in your system prompt. "
                "If you have both a Title and Time, call 'schedule_event' IMMEDIATELY."
            )
            result["_continue_chaining"] = True
            
        return result

    # --- 3. RETRIEVAL & DELETION ---
    if func_name == "get_all_events":
        return await calendar_service.request("GET", "/events")

    if func_name == "delete_event":
        if user_role != "admin": 
            return {"error": "Unauthorized: Admin role required for deletion."}
        return await calendar_service.request("DELETE", f"/events/{args.get('event_id')}")

    return {"error": f"Function {func_name} not implemented"}