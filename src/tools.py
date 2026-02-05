# src/tools.py

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_all_events",
            "description": "Retrieve all calendar events for the current legal tenant.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_by_id",
            "description": "Get detailed information about a specific legal event using its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The unique UUID of the calendar event."
                    }
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "schedule_event",
            "description": "Create a new legal event (e.g., Deposition, Hearing) on the calendar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Title of the event (e.g., Deposition of John Doe)"},
                    "start_time": {"type": "string", "description": "ISO 8601 formatted start time"},
                    "end_time": {"type": "string", "description": "ISO 8601 formatted end time"},
                    "description": {"type": "string", "description": "Additional notes or case references"}
                },
                "required": ["title", "start_time", "end_time"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Permanently remove an event from the calendar. Requires admin role and user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The UUID of the event to delete"},
                    "confirmed": {
                        "type": "boolean", 
                        "description": "Must be true if the user explicitly confirmed the deletion."
                    }
                },
                "required": ["event_id", "confirmed"]
            }
        }
    }
]