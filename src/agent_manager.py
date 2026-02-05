import json
from src.logger import logger
from src.agents.calendar_agent import handle_calendar

# ADD tenant_id to the arguments here
async def execute_tool_call(tool_call, services, user_role, tenant_id):
    func_name = tool_call.function.name
    args = json.loads(tool_call.function.arguments)
    
    # This was likely failing because tenant_id wasn't defined in this scope
    logger.warning(f"🤖 AGENT DECISION: {func_name} | Args: {args}")
    logger.info(f"AUDIT: Tenant {tenant_id} | User {user_role} | Action {func_name}")
    
    if func_name in ["get_all_events", "get_event_by_id", "schedule_event", "delete_event"]:
        result = await handle_calendar(func_name, args, services['calendar'], user_role)
        return result # Ensure this is returned!

    # 2. Add a fallback so it NEVER returns None
    logger.error(f"❌ No handler for tool: {func_name}")
    return {"error": f"Tool {func_name} not recognized", "status": 404}