# Schema for a standard timed meeting or appointment
STANDARD_EVENT_SCHEMA = [
    {"key": "title", "label": "Event Title", "required": True},
    {"key": "start_datetime", "label": "Start Time", "required": True},
    {"key": "end_datetime", "label": "Duration (e.g. 60m) or End Time", "required": True, "aliases": ["duration", "duration_minutes"]},
    {"key": "description", "label": "Description", "required": False},
    {"key": "location", "label": "Location", "required": False},
    {"key": "timezone", "label": "Timezone", "required": False, "suggest_from_context": "user_timezone_name"},
    {"key": "attendees", "label": "Attendees", "required": False, "type": "list"}
]

# Schema for an all-day event or deadline
ALL_DAY_EVENT_SCHEMA = [
    {"key": "title", "label": "Deadline Title", "required": True},
    {"key": "start_datetime", "label": "Start Date", "required": True},
    {"key": "end_datetime", "label": "End Date", "required": True},
    {"key": "description", "label": "Deadline Details", "required": False},
    {"key": "location", "label": "Location", "required": False}
]

# Keep generic for back-compat if needed
EVENT_SCHEMA = STANDARD_EVENT_SCHEMA
