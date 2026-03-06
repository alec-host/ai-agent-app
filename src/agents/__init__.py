from src.agents.calendar_agent import handle_calendar
from src.agents.client_creation_agent import handle_client_creation
from src.agents.rag_agent import handle_rag_lookup

# This makes it easier to manage exports as you add more domains
__all__ = ["handle_calendar", "handle_client_creation", "handle_rag_lookup"]
