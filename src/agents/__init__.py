from src.agents.calendar_agent import handle_calendar
from src.agents.rag_agent import handle_rag_lookup
from src.agents.core_agent import handle_core_ops

# This makes it easier to manage exports as you add more domains
__all__ = ["handle_calendar", "handle_core_ops", "handle_rag_lookup"]
