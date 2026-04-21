# src/dynamic_schema/task_schema.py

# Schema for a basic task that is standalone and not tied to a specific matter
BASIC_TASK_SCHEMA = [
    {"key": "name", "label": "Task Name", "required": True, "aliases": ["title", "task_name"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["details", "summary", "instructions"]},
    {"key": "priority_id", "label": "Priority", "required": True, "lookup_tool": "lookup_priority", "aliases": ["priority", "importance", "urgency", "priority_level"]},
    {"key": "task_category_id", "label": "Task Category", "required": True, "lookup_tool": "lookup_task_category", "aliases": ["category", "type", "task_type"]},
    
    # Scheduling fields (These are typically mapped to status_tracks in the API payload)
    {"key": "start_date", "label": "Start Date", "required": False, "aliases": ["start"]},
    {"key": "due_date", "label": "Due Date", "required": False, "aliases": ["deadline", "due"]},
    {"key": "due_time", "label": "Due Time", "required": False, "aliases": ["time"]},
    
    # Assignees & Subtasks (Optional array/list constructs)
    {"key": "assignees", "label": "Assignees", "required": False, "type": "list", "lookup_tool": "lookup_user", "aliases": ["assigned_to", "users", "team"]},
    {"key": "subtasks", "label": "Subtasks", "required": False, "type": "list", "aliases": ["checklist", "steps", "action_items"]}
]

# Schema for a matter-related task
MATTER_TASK_SCHEMA = [
    {"key": "name", "label": "Task Name", "required": True, "aliases": ["title", "task_name"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["details", "summary", "instructions"]},
    
    # Relational associations specific to Matters
    {"key": "client_id", "label": "Client", "required": True, "lookup_tool": "lookup_client", "aliases": ["client", "client_name"]},
    {"key": "matter_id", "label": "Matter", "required": True, "lookup_tool": "lookup_matter", "aliases": ["matter", "case", "file"]},
    
    {"key": "priority_id", "label": "Priority", "required": True, "lookup_tool": "lookup_priority", "aliases": ["priority", "importance", "urgency", "priority_level"]},
    {"key": "task_category_id", "label": "Task Category", "required": True, "lookup_tool": "lookup_task_category", "aliases": ["category", "type", "task_type"]},
    
    # Scheduling fields (These are typically mapped to status_tracks in the API payload)
    {"key": "start_date", "label": "Start Date", "required": False, "aliases": ["start"]},
    {"key": "due_date", "label": "Due Date", "required": False, "aliases": ["deadline", "due"]},
    {"key": "due_time", "label": "Due Time", "required": False, "aliases": ["time"]},
    
    # Assignees & Subtasks (Optional array/list constructs)
    {"key": "assignees", "label": "Assignees", "required": False, "type": "list", "lookup_tool": "lookup_user", "aliases": ["assigned_to", "users", "team"]},
    {"key": "subtasks", "label": "Subtasks", "required": False, "type": "list", "aliases": ["checklist", "steps", "action_items"]}
]
