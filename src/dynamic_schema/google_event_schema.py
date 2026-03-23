# src/dynamic_schema/google_event_schema.py

# Schema for a specialized Google Calendar event
# Using the snake_case keys for the logic layer and labels for UI display
GOOGLE_EVENT_SCHEMA = [
    {"key": "title", "label": "Event Title", "required": True},
    {"key": "startTime", "label": "Start Date/Time (ISO)", "required": True},
    {"key": "duration_minutes", "label": "Duration (min)", "required": False, "default": 60},
    {"key": "summary", "label": "Brief Summary/Description", "required": False},
    {"key": "location", "label": "Meeting Venue/Link", "required": False},
    {"key": "attendees", "label": "Attendees", "required": False, "type": "list"}
]
