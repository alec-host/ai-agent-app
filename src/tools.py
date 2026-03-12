# src/tools.py

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_event",
            "description": "Schedules or DRAFTS a calendar event. Call this IMMEDIATELY as soon as you have ANY detail (like just the title or just the time) to save progress to the database vault.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Title of the event (e.g., Deposition of John Doe). NEVER assume 'Consultation', if unknown, ask the user."},
                    "startTime": {"type": "string", "description": "ISO 8601 formatted start time (e.g., 2026-02-10T14:00:00Z)"},
                    "endTime": {"type": "string", "description": "ISO 8601 formatted end time. Optional if duration_minutes is provided."},
                    "duration_minutes": {"type": "integer", "description": "Duration of the event in minutes."},
                    "description": {"type": "string", "description": "The agenda or detailed summary of the event."},
                    "location": {"type": "string", "description": "The physical or virtual venue for the meeting."},
                    "attendees": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "A JSON array of valid email addresses representing the guests to invite."
                    },
                    "isAllDay": {"type": "boolean", "description": "Set to true for all-day events or deadlines."},
                    "date": {"type": "string", "description": "Format YYYY-MM-DD. Use ONLY if isAllDay is true."}
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "initialize_calendar_session",
            "description": "Verifies or generates a fresh access token for the tenant. Call this before first-time calendar access. Pass summary/startTime if already known to preserve context.",
            "parameters": {
               "type": "object",
               "properties": {
                    "tenant_id": {
                        "type": "string",
                        "description": "The unique UUID of a tenant."
                    },
                    "summary": {
                        "type": "string",
                        "description": "The title discussed so far (to keep in memory)."
                    },
                    "startTime": {
                        "type": "string",
                        "description": "The time discussed so far (to keep in memory)."
                    }             
               },
               "required": ["tenant_id"]
            }
        } 
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_status",
            "description": "Check the health and connectivity of the legal calendar microservices.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_calendar_connection",
            "description": "Checks if the user's Google Calendar is connected and authorized.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_all_events",
            "description": "Retrieve all calendar events for the current legal tenant.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_by_id",
            "description": "Get detailed information about a specific legal event using its ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {
                        "type": "string",
                        "description": "The unique UUID of the calendar event."
                    }
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "update_event",
            "description": "Modify an existing event (change time, title, or description).",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The UUID of the event to modify"},
                    "summary": {"type": "string", "description": "New title for the event"},
                    "startTime": {"type": "string", "description": "New ISO 8601 start time"},
                    "endTime": {"type": "string", "description": "New ISO 8601 end time"},
                    "description": {"type": "string", "description": "Updated notes"}
                },
                "required": ["event_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "delete_event",
            "description": "Permanently remove an event. Requires admin role and explicit user confirmation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "string", "description": "The UUID of the event to delete"},
                    "confirmed": {
                        "type": "boolean", 
                        "description": "Must be true if the user explicitly said 'Yes' or 'Confirmed'."
                    }
                },
                "required": ["event_id", "confirmed"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_firm_protocol",
            "description": "Searches firm guidelines for matter intake, practice area mapping, and summarization rules.",
            "parameters": {
                "type": "object",
                "properties": {
                   "query": {"type": "string", "description": "The workflow step Nuru needs help with."}
                }
            }
        }
    },
    { 
       "type": "function",
       "function": {
           "name": "search_knowledge_base",
           "description": "Search for firm policies, legal templates, or case notes to provide accurate answers.",
           "parameters": {
              "type": "object",
              "properties": {
                 "query": {"type": "string", "description": "The specific topic or question to research."}
              },
              "required": ["query"]
           }
      }
   },
    {
      "type": "function",
      "function": {
          "name": "create_client_record",
          "description": "Saves or DRAFTS a client record. Call this IMMEDIATELY as soon as you have ANY field. If you have multiple fields (e.g., First and Last name), you MUST pass them all in a SINGLE call.",
          "parameters": {
             "type": "object",
             "properties": {
                 "client_number": {"type": "string", "description": "The unique identifier or ID assigned to the client."},
                 "client_type": {"type": "string", "description": "The category of the client (e.g., individual, corporate, associate)."},
                 "first_name": {"type": "string", "description": "The client's legal first name."},
                 "last_name": {"type": "string", "description": "The client's legal last name."},
                 "email": {"type": "string", "description": "The primary contact email address for the client."}
             }
          }
      }
   },
    {
       "type": "function",
       "function": {
           "name": "authenticate_to_core",
           "description": "Authenticates the user into the MatterMiner Core remote system using email and password. Call this when the user needs to access their matters, cases, or profile.",
           "parameters": {
               "type": "object",
               "properties": {
                   "email": {"type": "string", "description": "The user's login email address."},
                   "password": {"type": "string", "description": "The user's secret password."}
               },
               "required": ["email", "password"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "create_contact",
           "description": "Saves or DRAFTS a contact record in the MatterMiner Core system. Call this IMMEDIATELY as soon as you have ANY piece of information (like first name or email) to save progress to the database vault.",
           "parameters": {
               "type": "object",
               "properties": {
                   "contact_type": {"type": "string", "description": "Type of contact (e.g., primary, secondary)."},
                   "title": {"type": "string", "description": "Honorific title (e.g., Mr., Ms., Dr.)."},
                   "first_name": {"type": "string", "description": "The contact's first name."},
                   "middle_name": {"type": "string", "description": "The contact's middle name."},
                   "last_name": {"type": "string", "description": "The contact's last name."},
                   "email": {"type": "string", "description": "The contact's email address."},
                   "country_code": {"type": "string", "description": "International dialing code (e.g., +1, +254)."},
                   "phone_number": {"type": "string", "description": "The contact's phone number."},
                   "model_type": {"type": "string", "description": "Associated model (e.g., App\\Models\\Prospect)."},
                   "model_id": {"type": "integer", "description": "The ID of the associated model."},
                   "active": {"type": "boolean", "description": "Whether the contact is active."},
                   "featured": {"type": "boolean", "description": "Whether the contact is featured."}
               }
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "lookup_countries",
           "description": "Searches for country information (name, id, etc.) in the MatterMiner Core system. Use this to find the correct country_id for client or contact creation.",
           "parameters": {
               "type": "object",
               "properties": {
                   "search": {"type": "string", "description": "The country name or keyword to search for."},
                   "page": {"type": "integer", "description": "Page number for results (default 1)."},
                   "per_page": {"type": "integer", "description": "Number of results per page (default 15)."}
               }
           }
       }
    }
]


