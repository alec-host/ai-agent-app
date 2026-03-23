# src/dynamic_schema/contact_schema.py

# A dynamic schema for contact creation.
CONTACT_SCHEMA = [
    {"key": "first_name", "label": "First Name", "required": True, "aliases": ["firstName", "given_name", "givenName"]},
    {"key": "last_name", "label": "Last Name", "required": True, "aliases": ["lastName", "surname", "family_name", "familyName"]},
    {"key": "client_email", "label": "Email Address", "required": True, "aliases": ["EmailAddress", "client_email", "email", "contact_email"]},
    {"key": "contact_type", "label": "Contact Type", "required": False, "default": "primary", "system_only": True},
    {"key": "title", "label": "Title", "required": True, "aliases": ["honorific", "salutation"], "choices": ["Mr.", "Ms.", "Mrs.", "Dr.", "Prof."]},
    {"key": "middle_name", "label": "Middle Name", "required": False, "aliases": ["middleName"]},
    {"key": "country_code", "label": "Country Code", "required": True, "aliases": ["countryCode", "country_id", "countryId", "dialling_code"]},
    {"key": "phone_number", "label": "Phone Number", "required": True, "aliases": ["phoneNumber", "mobile", "tel", "cell"]},
    {"key": "model_type", "label": "Model Type", "required": False, "default": "App\\Models\\Prospect", "system_only": True},
    {"key": "model_id", "label": "Model ID", "required": False, "default": 1, "system_only": True},
    {"key": "active", "label": "Active Status", "required": False, "default": True, "system_only": True},
    {"key": "featured", "label": "Featured Status", "required": False, "default": False, "system_only": True}
]
