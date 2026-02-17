# src/tools.py

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_event",
            "description": "Schedules a new calendar event. Use for specific times or all-day legal dates like holidays.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Title of the event (e.g., Deposition of John Doe)"},
                    "startTime": {"type": "string", "description": "ISO 8601 formatted start time (e.g., 2026-02-10T14:00:00Z)"},
                    "endTime": {"type": "string", "description": "ISO 8601 formatted end time. Optional if duration_minutes is provided."},
                    "duration_minutes": {"type": "integer", "description": "Duration of the event in minutes."},
                    "description": {"type": "string", "description": "Additional notes or case references"},
                    "isAllDay": {"type": "boolean", "description": "Set to true for all-day events or deadlines."},
                    "date": {"type": "string", "description": "Format YYYY-MM-DD. Use ONLY if isAllDay is true."}
                },
                "required": ["summary", "startTime"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "initialize_calendar_session",
            "description": "Verifies or generates a fresh access token for the tenant. Call this before first-time calendar access. Pass summary/startTime if already known to preserve context.",
            "parameters": {
               "type": "object",
               "properties": {
                    "tenant_id": {
                        "type": "string",
                        "description": "The unique UUID of a tenant."
                    },
                    "summary": {
                        "type": "string",
                        "description": "The title discussed so far (to keep in memory)."
                    },
                    "startTime": {
                        "type": "string",
                        "description": "The time discussed so far (to keep in memory)."
                    }             
               },
               "required": ["tenant_id"]
            }
        } 
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "Check the health and connectivity of the legal calendar microservices.",
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
            "name": "check_calendar_connection",
            "description": "Checks if the user's Google Calendar is connected and authorized.",
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
            "name": "update_event",
            "description": "Modify an existing event (change time, title, or description).",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The UUID of the event to modify"},
                    "summary": {"type": "string", "description": "New title for the event"},
                    "startTime": {"type": "string", "description": "New ISO 8601 start time"},
                    "endTime": {"type": "string", "description": "New ISO 8601 end time"},
                    "description": {"type": "string", "description": "Updated notes"}
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Permanently remove an event. Requires admin role and explicit user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The UUID of the event to delete"},
                    "confirmed": {
                        "type": "boolean", 
                        "description": "Must be true if the user explicitly said 'Yes' or 'Confirmed'."
                    }
                },
                "required": ["event_id", "confirmed"]
            }
        }
    }
]