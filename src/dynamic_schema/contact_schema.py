# src/dynamic_schema/contact_schema.py

# A dynamic schema for contact creation.
CONTACT_SCHEMA = [
    {"key": "first_name", "label": "First Name", "required": True, "aliases": ["firstName"]},
    {"key": "last_name", "label": "Last Name", "required": True, "aliases": ["lastName"]},
    {"key": "email", "label": "Email Address", "required": True, "aliases": ["emailAddress"]},
    {"key": "contact_type", "label": "Contact Type", "required": False, "default": "primary"},
    {"key": "title", "label": "Title", "required": False},
    {"key": "middle_name", "label": "Middle Name", "required": False},
    {"key": "country_code", "label": "Country Code", "required": False},
    {"key": "phone_number", "label": "Phone Number", "required": False},
    {"key": "model_type", "label": "Model Type", "required": False},
    {"key": "model_id", "label": "Model ID", "required": False},
    {"key": "active", "label": "Active", "required": False, "default": True},
    {"key": "featured", "label": "Featured", "required": False, "default": False},
    {"key": "country_id", "label": "Country ID", "required": False}
]
