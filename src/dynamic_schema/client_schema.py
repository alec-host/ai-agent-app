# src/dynamic_schema/client_schema.py

# A dynamic schema that dictates the fields required for client registration.
# 'key' is the internal variable name used by the backend.
# 'label' is the human-readable string used in AI prompts and response tracking.
# 'required' dictates if the intake gating stops to ask for it.
# 'aliases' are common variations the AI might use when returning function call arguments.
CLIENT_SCHEMA = [
    {"key": "first_name", "label": "First Name", "required": True, "aliases": ["firstName"]},
    {"key": "last_name", "label": "Last Name", "required": True, "aliases": ["lastName"]},
    {"key": "client_type", "label": "Client Type", "required": True, "aliases": ["clientType", "type", "customer_type", "customerType"]},
    {"key": "client_email", "label": "Client Email", "required": True, "aliases": ["email","client_email", "clientEmail", "client_email_address", "clientEmailAddress"]}
]
