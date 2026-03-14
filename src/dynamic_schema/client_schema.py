# src/dynamic_schema/client_schema.py

# A dynamic schema that dictates the fields required for client registration.
# 'key' is the internal variable name used by the backend.
# 'label' is the human-readable string used in AI prompts and response tracking.
# 'required' dictates if the intake gating stops to ask for it.
# 'aliases' are common variations the AI might use when returning function call arguments.
CLIENT_SCHEMA = [
    {"key": "first_name", "label": "First Name", "required": True, "aliases": ["firstName"]},
    {"key": "last_name", "label": "Last Name", "required": True, "aliases": ["lastName"]},
    {"key": "client_number", "label": "Client Number", "required": True, "aliases": ["clientNumber", "customer_number", "customerNumber", "number"]},
    {"key": "client_type", "label": "Client Type", "required": True, "aliases": ["clientType", "type", "customer_type", "customerType"]},
    {"key": "email", "label": "Email", "required": True, "aliases": ["email_address", "emailAddress"]}
]
