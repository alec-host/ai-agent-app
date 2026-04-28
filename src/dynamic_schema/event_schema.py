# Schema for a standard timed meeting or appointment
STANDARD_EVENT_SCHEMA = [
    {"key": "title", "label": "Title", "required": True},
    {"key": "meeting_date", "label": "Date of the Meeting", "required": True, "aliases": ["date"]},
    {"key": "start_time", "label": "Start Time", "required": True, "aliases": ["start"]},
    {"key": "end_time", "label": "End Time or Duration", "required": True, "aliases": ["end", "duration", "duration_minutes"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["summary", "body", "details", "notes"]},
    {"key": "location", "label": "Location", "required": False, "aliases": ["Physical Address", "Zoom", "Google Meet","Teams"]},
    {"key": "timezone", "label": "Timezone", "required": True, "suggest_from_context": "user_timezone_name"},
    {"key": "is_matter_related", "label": "Related to Matter (Yes/No)", "required": True, "type": "choice", "choices": ["Yes", "No"]},
    {"key": "client_id", "label": "Client ID", "required": True, "depends_on": {"key": "is_matter_related", "value": "Yes"}},
    {"key": "matter_id", "label": "Matter ID", "required": True, "depends_on": {"key": "is_matter_related", "value": "Yes"}},
    {"key": "visibility", "label": "Visibility", "required": False, "default": "private"},
    {"key": "status", "label": "Status", "required": False, "default": "confirmed"},
    {"key": "reminders", "label": "Reminders", "required": False, "type": "list", "default": [{"method": "email", "minutes": 60}, {"method": "popup", "minutes": 15}]},
    {"key": "attendees", "label": "Attendees", "required": True, "type": "list"}
]

# Schema for an all-day event or deadline
ALL_DAY_EVENT_SCHEMA = [
    {"key": "title", "label": "Title", "required": True},
    {"key": "meeting_date", "label": "Date of the Event", "required": True, "aliases": ["start_date", "date"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["summary", "body", "details", "notes"]},
    {"key": "timezone", "label": "Timezone", "required": True, "suggest_from_context": "user_timezone_name"},
    {"key": "location", "label": "Location", "required": False, "aliases": ["Physical Address", "Zoom", "Google Meet","Teams"]},
    {"key": "visibility", "label": "Visibility", "required": False, "default": "private"},
    {"key": "status", "label": "Status", "required": False, "default": "confirmed"},
    {"key": "is_matter_related", "label": "Related to Matter (Yes/No)", "required": True, "type": "choice", "choices": ["Yes", "No"]},
    {"key": "client_id", "label": "Client ID", "required": True, "depends_on": {"key": "is_matter_related", "value": "Yes"}},
    {"key": "matter_id", "label": "Matter ID", "required": True, "depends_on": {"key": "is_matter_related", "value": "Yes"}},
    {"key": "reminders", "label": "Reminders", "required": False, "type": "list", "default": [{"method": "email", "minutes": 1440}]}
]

# Keep generic for back-compat if needed
EVENT_SCHEMA = STANDARD_EVENT_SCHEMA