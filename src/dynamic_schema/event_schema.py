# src/dynamic_schema/event_schema.py

EVENT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": "The title of the event (e.g., 'Client Meeting - Case Review')."
        },
        "start_datetime": {
            "type": "string",
            "description": "The ISO 8601 formatted start date and time (e.g., '2025-01-20T10:00:00')."
        },
        "end_datetime": {
            "type": "string",
            "description": "The ISO 8601 formatted end date and time (e.g., '2025-01-20T11:30:00')."
        },
        "description": {
            "type": "string",
            "description": "A detailed summary or agenda for the event."
        },
        "location": {
            "type": "string",
            "description": "The physical or virtual location of the event."
        },
        "timezone": {
            "type": "string",
            "description": "The timezone for the event (e.g., 'America/New_York'). Defaults to 'UTC' if not specified."
        },
        "is_all_day": {
            "type": "boolean",
            "description": "Whether the event lasts the entire day."
        },
        "matter_id": {
            "type": ["string", "integer", "null"],
            "description": "The unique ID of the legal matter associated with this event."
        },
        "visibility": {
            "type": "string",
            "enum": ["public", "private", "busy"],
            "description": "The visibility level of the event."
        },
        "status": {
            "type": "string",
            "enum": ["confirmed", "tentative", "cancelled"],
            "description": "The current status of the event."
        },
        "attendees": {
            "type": "array",
            "items": {"type": "string"},
            "description": "A list of email addresses of people attending the event."
        },
        "reminders": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["email", "popup"]},
                    "minutes": {"type": "integer"}
                }
            },
            "description": "Automatic notification rules for the event."
        }
    },
    "required": ["title", "start_datetime", "end_datetime"]
}
