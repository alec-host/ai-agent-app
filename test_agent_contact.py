import asyncio
import logging
from src.agents.core_agent import handle_create_contact
from src.services.calendar_state import CalendarService

logging.basicConfig(level=logging.INFO)

async def test_agent():
    # We need a mock db session/calendar service
    from unittest.mock import AsyncMock
    mock_calendar = AsyncMock()
    
    # Return empty session to avoid rehydration
    mock_calendar.get_client_session.return_value = {}
    
    services = {
        'calendar': mock_calendar
    }
    
    history = []
    
    # Try passing the COMPLETE payload to see if it triggers API
    args = {
        "title": "Mr.",
        "first_name": "Jill",
        "last_name": "Bill",
        "client_email": "jill.bill@yopmail.com",
        "contact_type": "primary",
        "country_code": "254",
        "phone_number": "1234567"
    }

    res = await handle_create_contact(
        args=args,
        services=services,
        tenant_id="12345678",
        history=history,
        user_email="dev@matterminer.com"
    )
    
    print("Result:")
    import json
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    asyncio.run(test_agent())
