# src/dynamic_schema/event_schema.py

# Schema for a standard timed meeting or appointment
STANDARD_EVENT_SCHEMA = [
    {"key": "title", "label": "Event Title", "required": True},
    {"key": "start_datetime", "label": "Start Time", "required": True},
    {"key": "end_datetime", "label": "End Time", "required": True},
    {"key": "description", "label": "Description", "required": False},
    {"key": "location", "label": "Location", "required": False},
    {"key": "timezone", "label": "Timezone", "required": False, "default": "UTC"},
    {"key": "attendees", "label": "Attendees", "required": False, "type": "list"}
]

# Schema for an all-day event or deadline
ALL_DAY_EVENT_SCHEMA = [
    {"key": "title", "label": "Deadline Title", "required": True},
    {"key": "start_datetime", "label": "Start Date", "required": True},
    {"key": "end_datetime", "label": "End Date", "required": True},
    {"key": "description", "label": "Deadline Details", "required": False}
]

# Keep generic for back-compat if needed, but the AI should prefer the specific ones
EVENT_SCHEMA = STANDARD_EVENT_SCHEMA
