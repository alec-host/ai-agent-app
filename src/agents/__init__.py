from src.agents.calendar_agent import handle_calendar
from src.agents.client_creation_agent import handle_client_intake_partial
# from .billing_agent import handle_billing

# This makes it easier to manage exports as you add more domains
__all__ = ["handle_calendar", "handle_client_intake_partial"]
