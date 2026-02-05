from src.logger import logger

async def handle_calendar(func_name, args, calendar_service, user_role):
    # Log that we entered the domain agent
    logger.info(f"Calendar Agent handling: {func_name}")

    if func_name == "get_all_events":
        return await calendar_service.request("GET", "/events")

    if func_name == "get_event_by_id":
        event_id = args.get('event_id')
        if not event_id:
            return {"error": "Missing event_id"}
        return await calendar_service.request("GET", f"/events/{event_id}")

    if func_name == "schedule_event":
        return await calendar_service.request("POST", "/events", args)

    if func_name == "delete_event":
        if user_role != "admin":
            return {"response": "Access Denied: Only administrators can delete events."}
        
        if not args.get("confirmed"):
            return {"response": "I need your explicit confirmation to delete this event. Should I proceed?"}
        
        return await calendar_service.request("DELETE", f"/events/{args['event_id']}")

    # Final fallback: Instead of return None, return an error dict
    return {"error": f"Function {func_name} not implemented in Calendar Agent"}