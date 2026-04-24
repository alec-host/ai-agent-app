# src/dynamic_schema/matter_schema.py

# A dynamic schema that dictates the fields required for matter creation.
MATTER_SCHEMA = [
    {"key": "title", "label": "Matter Title", "required": True, "aliases": ["matter_title", "title", "subject"]},
    {"key": "name", "label": "Matter Name", "required": True, "aliases": ["matter_name", "name", "internal_name", "reference"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["details", "matter_description", "summary"]},
    {"key": "client_id", "label": "Client", "required": True, "lookup_tool": "lookup_client", "aliases": ["client", "client_name", "client_email"]},
    {"key": "practice_area_id", "label": "Practice Area", "required": True, "lookup_tool": "lookup_practice_area", "is_dynamic": True, "aliases": ["practice_area", "category", "area"]},
    {"key": "case_stage_id", "label": "Case Stage", "required": True, "lookup_tool": "lookup_case_stage", "is_dynamic": True, "aliases": ["stage", "case_stage", "status"]},
    {"key": "billing_type_id", "label": "Billing Type", "required": True, "lookup_tool": "lookup_billing_type", "is_dynamic": True, "aliases": ["billing", "billing_type", "fee_structure"]},
    {"key": "access_type", "label": "Access Level", "required": True, "choices": ["public", "restricted"], "aliases": ["access", "visibility"]},
    {"key": "lawyer_assignments", "label": "Assigned Lawyers", "required": True, "type": "list", "lookup_tool": "lookup_user", "aliases": ["lawyers", "attorneys"]},
    {"key": "matter_users", "label": "Matter Users", "required": False, "type": "list", "lookup_tool": "lookup_user", "aliases": ["users", "assigned_users"]},
    {"key": "matter_groups", "label": "Matter Groups", "required": False, "type": "list", "lookup_tool": "lookup_group", "aliases": ["groups"]},
    {"key": "limitation_statutes", "label": "Limitation Statutes", "required": False, "type": "list", "aliases": ["limitations", "statute_of_limitations", "deadlines"]},
    {"key": "access_types", "label": "Complex Access", "required": False, "type": "list", "system_only": True}
]
