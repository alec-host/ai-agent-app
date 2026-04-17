# src/dynamic_schema/matter_schema.py

# A dynamic schema that dictates the fields required for matter creation.
MATTER_SCHEMA = [
    {"key": "title", "label": "Matter Title", "required": True, "aliases": ["matter_title", "title", "name", "matter_name"]},
    {"key": "name", "label": "Matter Name", "required": True, "aliases": ["matter_name", "name", "title", "matter_title"]},
    {"key": "description", "label": "Description", "required": True, "aliases": ["details", "matter_description", "summary"]},
    {"key": "client_id", "label": "Client", "required": True, "lookup_tool": "lookup_client", "aliases": ["client", "client_name", "client_email"]},
    {"key": "practice_area_id", "label": "Practice Area", "required": True, "lookup_tool": "lookup_practice_area", "is_dynamic": True, "aliases": ["practice_area", "category", "area"]},
    {"key": "case_stage_id", "label": "Case Stage", "required": True, "lookup_tool": "lookup_case_stage", "is_dynamic": True, "aliases": ["stage", "case_stage", "status"]},
    {"key": "billing_type_id", "label": "Billing Type", "required": True, "lookup_tool": "lookup_billing_type", "aliases": ["billing", "billing_type", "fee_structure"]}
]
