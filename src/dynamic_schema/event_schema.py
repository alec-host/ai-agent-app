# Schema for a standard timed meeting or appointment
STANDARD_EVENT_SCHEMA = [
    {"key": "title", "label": "Title", "required": True},
    {"key": "start_datetime", "label": "Start Time", "required": True, "aliases": ["start_date", "date", "start_time", "start"]},
    {"key": "end_datetime", "label": "Duration (e.g. 60m) or End Time", "required": True, "aliases": ["duration", "duration_minutes", "end_date", "end_time", "end"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["summary", "body", "details", "notes"]},
    {"key": "location", "label": "Location", "required": False, "aliases": ["Physical Address", "Zoom", "Google Meet","Teams"]},
    {"key": "timezone", "label": "Timezone", "required": True, "suggest_from_context": "user_timezone_name"},
    {"key": "matter_id", "label": "Matter", "required": False},
    {"key": "visibility", "label": "Visibility", "required": False, "default": "private"},
    {"key": "status", "label": "Status", "required": False, "default": "confirmed"},
    {"key": "reminders", "label": "Reminders", "required": False, "type": "list", "default": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
    {"key": "attendees", "label": "Attendees", "required": True, "type": "list"}
]

# Schema for an all-day event or deadline
ALL_DAY_EVENT_SCHEMA = [
    {"key": "title", "label": "Title", "required": True},
    {"key": "start_datetime", "label": "Start Date", "required": True, "aliases": ["start_date", "date", "start"]},
    {"key": "end_datetime", "label": "End Date", "required": True, "aliases": ["end_date", "end"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["summary", "body", "details", "notes"]},
    {"key": "location", "label": "Location", "required": False, "aliases": ["Physical Address", "Zoom", "Google Meet","Teams"]},
    {"key": "visibility", "label": "Visibility", "required": False, "default": "private"},
    {"key": "status", "label": "Status", "required": False, "default": "confirmed"},
    {"key": "reminders", "label": "Reminders", "required": False, "type": "list", "default": [{"method": "email", "minutes": 1440}]}
]

# Keep generic for back-compat if needed
EVENT_SCHEMA = STANDARD_EVENT_SCHEMA