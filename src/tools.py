# src/tools.py

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "schedule_event",
            "description": "Schedules or DRAFTS an event on the user's EXTERNAL Google Calendar. Call this ONLY if 'Google' or 'Personal' calendar is explicitly mentioned. NEVER use this for standard MatterMiner firm appointments.",
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
            "description": "Handshake for EXTERNAL Google Calendar access. Call this ONLY if the user specifically requests to use their Google/Personal calendar. DO NOT call this for MatterMiner Core events.",
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
            "name": "delete_event",
            "description": "Removes a specific calendar event using its unique ID.",
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
            "name": "clear_calendar_session",
            "description": "Forcefully destroys the current user session and disconnects Google Calendar.",
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
            "name": "lookup_firm_protocol",
            "description": "Consult the official firm rulebook for guidance on specific operational steps (e.g., 'How do I add a new client?').",
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
                 "client_email": {"type": "string", "description": "The primary client's email address."},
                 "first_name": {"type": "string", "description": "The client's legal first name. NEVER use an alphanumeric ID here."},
                 "last_name": {"type": "string", "description": "The client's legal last name. NEVER use an alphanumeric ID here."},
                 "client_type": {"type": "string", "enum": ["individual", "company"], "description": "The category of the client."},
                 "contact_id": {"type": "string", "description": "Relational link: The UUID of the contact record obtained via lookup or creation."},
                 "country_id": {"type": "integer", "description": "Relational link: The ID of the country obtained via lookup_countries."},
                 "street": {"type": "string", "description": "The client's physical street address."}
             }
          }
      }
   },
    {
       "type": "function",
       "function": {
           "name": "promote_contact_to_client",
           "description": "Converts an existing contact record into a formal client profile. Use this when you have a contact_id but need to finalize client-specific details.",
           "parameters": {
              "type": "object",
              "properties": {
                  "contact_id": {"type": "string", "description": "The UUID of the existing contact."},
                   "client_type": {"type": "string", "enum": ["individual", "company"], "description": "The category of the profile."},
                  "country_id": {"type": "integer", "description": "The ID of the country (lookup via lookup_countries)."},
                  "street": {"type": "string", "description": "The client's physical address."}
              },
              "required": ["contact_id", "client_type", "country_id", "street"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "create_contact",
           "description": "Saves or DRAFTS a contact record in the MatterMiner Core system. Aligned with natural conversational flow (Title -> First Name -> Last Name). Call this IMMEDIATELY when the user wants to create a contact, even if you have NO information yet, so the gating system can guide you strictly one step at a time. CRITICAL: You MUST call this tool EVERY TIME the user provides a newly requested field (e.g., Last Name) to update the draft. Do NOT just reply with text.",
           "parameters": {
               "type": "object",
               "properties": {
                    "title": {"type": "string", "description": "Honorific title (Mr, Dr, etc). MUST NOT BE GUESSED."},
                    "first_name": {"type": "string", "description": "Legal FIRST name. NEVER guess or split from email. ONLY populate if explicitly stated."},
                    "middle_name": {"type": "string", "description": "Legal MIDDLE name (if any)."},
                    "last_name": {"type": "string", "description": "Legal LAST name. NEVER guess or split from email. ONLY populate if explicitly stated."},
                    "contact_type": {"type": "string", "enum": ["primary", "secondary"], "description": "Type of contact. MUST NOT BE GUESSED."},
                    "client_email": {"type": "string", "description": "Valid email address."},
                    "country_code": {"type": "string", "description": "NUMERIC dialling code ONLY (e.g. valid dialling code). NEVER put a country name here."},
                    "phone_number": {"type": "string", "description": "Local phone number WITHOUT the country code."},
                    "model_type": {"type": "string", "description": "INTERNAL - DO NOT SET."},
                    "model_id": {"type": "integer", "description": "INTERNAL - DO NOT SET."},
                    "active": {"type": "boolean", "description": "INTERNAL - DO NOT SET."},
                    "featured": {"type": "boolean", "description": "INTERNAL - DO NOT SET."}
               }
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "search_contact_by_email",
           "description": "Searches the MatterMiner Core database for an existing contact using their email address. Use this when you need a contact_id, or to verify if a contact already exists.",
           "parameters": {
               "type": "object",
               "properties": {
                   "email": {"type": "string", "description": "The email address of the contact to look up."}
               },
               "required": ["email"]
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
    },
    {
       "type": "function",
       "function": {
           "name": "create_standard_event",
           "description": "Creates a standard timed meeting in the INTERNAL MatterMiner Core system. Use this as the default for all firm/matter-related appointments.",
           "parameters": {
               "type": "object",
               "properties": {
                   "title": {"type": "string", "description": "The title of the meeting."},
                   "start_datetime": {"type": "string", "description": "ISO 8601 start time (e.g. 2025-01-20T10:00:00)."},
                   "end_datetime": {"type": "string", "description": "ISO 8601 end time (e.g. 2025-01-20T11:30:00)."},
                   "description": {"type": "string", "description": "Agenda or notes."},
                   "location": {"type": "string", "description": "Venue or virtual link."},
                   "timezone": {"type": "string", "description": "Timezone (e.g. America/New_York). IMPORTANT: If unknown, present the common choices from your instructions and ask for a selection."},
                   "matter_id": {"type": ["integer", "null"], "description": "Relational matter ID if this event belongs to a firm matter. Otherwise null."},
                   "visibility": {"type": "string", "enum": ["private", "public", "restricted"], "description": "Event visibility. Default is private."},
                   "status": {"type": "string", "description": "Event status, default is confirmed."},
                   "reminders": {
                       "type": "array",
                       "items": {"type": "object", "properties": {"method": {"type": "string"}, "minutes": {"type": "integer"}}},
                       "description": "List of reminders. E.g. [{\"method\": \"email\", \"minutes\": 60}]"
                   },
                   "attendees": {
                       "type": "array",
                       "items": {"type": "string"},
                       "description": "List of attendee emails."
                   }
               },
               "required": ["title", "start_datetime", "end_datetime"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "create_all_day_event",
           "description": "Creates an all-day deadline in the INTERNAL MatterMiner Core system. Use this for all firm/matter-related deadlines.",
           "parameters": {
               "type": "object",
               "properties": {
                   "title": {"type": "string", "description": "The title of the deadline or event."},
                   "start_datetime": {"type": "string", "description": "ISO 8601 start time (e.g. 2025-01-25T00:00:00)."},
                   "end_datetime": {"type": "string", "description": "ISO 8601 end time (e.g. 2025-01-25T23:59:59)."},
                   "description": {"type": "string", "description": "Details about the deadline."},
                   "location": {"type": "string", "description": "Where the event/deadline takes place."},
                   "visibility": {"type": "string", "enum": ["private", "public", "restricted"], "description": "Event visibility. Default is private."},
                   "status": {"type": "string", "description": "Event status, default is confirmed."},
                   "reminders": {
                       "type": "array",
                       "items": {"type": "object", "properties": {"method": {"type": "string"}, "minutes": {"type": "integer"}}},
                       "description": "List of reminders. E.g. [{\"method\": \"email\", \"minutes\": 1440}]"
                   },
                   "attendees": {"type": "array", "items": {"type": "string"}, "description": "List of emails to invite."}
               },
               "required": ["title", "start_datetime", "end_datetime"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "lookup_client",
           "description": "Searches for a client in the system to retrieve the client_id. ONLY invoke this tool if the current workflow specifically requests it, or if the user explicitly provides a search term for it. Do NOT invoke proactively.",
           "parameters": {
               "type": "object",
               "properties": {
                   "search_term": {"type": "string", "description": "The client name or email to search for."}
               },
               "required": ["search_term"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "lookup_practice_area",
           "description": "Searches for a practice area to retrieve the practice_area_id. ONLY invoke this tool if the current workflow specifically requests it, or if the user explicitly provides a search term for it. Do NOT invoke proactively.",
           "parameters": {
               "type": "object",
               "properties": {
                   "search_term": {"type": "string", "description": "The practice area name to search for (e.g. Contract Dispute)."}
               },
               "required": ["search_term"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "lookup_case_stage",
           "description": "Searches for a case stage to retrieve the case_stage_id. ONLY invoke this tool if the current workflow specifically requests it, or if the user explicitly provides a search term for it. Do NOT invoke proactively.",
           "parameters": {
               "type": "object",
               "properties": {
                   "search_term": {"type": "string", "description": "The case stage to search for (e.g. Initial Contact)."}
               },
               "required": ["search_term"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "lookup_billing_type",
           "description": "Searches for a billing type to retrieve the billing_type_id. ONLY invoke this tool if the current workflow specifically requests it, or if the user explicitly provides a search term for it. Do NOT invoke proactively.",
           "parameters": {
               "type": "object",
               "properties": {
                   "search_term": {"type": "string", "description": "The billing type to search for (e.g. Hourly, Contingency)."}
               },
               "required": ["search_term"]
           }
       }
    },
    {
       "type": "function",
       "function": {
           "name": "create_matter",
           "description": "Saves or DRAFTS a matter record in the MatterMiner Core system. Call this IMMEDIATELY when the user wants to create a matter, even if you have NO information yet, so the gating system can guide you strictly one step at a time. CRITICAL: You MUST call this tool EVERY TIME the user provides a newly requested field (e.g., Matter Title) to update the draft. Do NOT just reply with text.",
           "parameters": {
               "type": "object",
               "properties": {
                   "title": {"type": "string", "description": "The title of the matter."},
                   "name": {"type": "string", "description": "The internal name reference for the matter."},
                   "client_id": {"type": "integer", "description": "The ID of the client (obtained via lookup_client)."},
                   "practice_area_id": {"type": "integer", "description": "The ID of the practice area (obtained via lookup_practice_area)."},
                   "description": {"type": "string", "description": "The details or summary of the matter."},
                   "case_stage_id": {"type": "integer", "description": "The ID of the case stage (obtained via lookup_case_stage)."},
                   "billing_type_id": {"type": "integer", "description": "The ID of the billing type (obtained via lookup_billing_type)."},
                   "access_type": {"type": "string", "enum": ["restricted", "public"]},
                   "lawyer_assignments": {"type": "array", "items": {"type": "integer"}, "description": "Array of Lawyer User IDs"},
                   "matter_users": {"type": "array", "items": {"type": "integer"}, "description": "Array of Matter User IDs"},
                   "matter_groups": {"type": "array", "items": {"type": "integer"}, "description": "Array of Group IDs"},
                   "limitation_statutes": {
                       "type": "array", 
                       "items": {
                           "type": "object", 
                           "properties": {
                                "name": {"type": "string"},
                                "description": {"type": "string"},
                                "due_at": {"type": "string", "description": "ISO date"}
                           }
                       }
                   }
               },
               "required": ["title", "name", "client_id", "practice_area_id", "description", "access_type", "case_stage_id", "billing_type_id", "lawyer_assignments"]
           }
       }
    },
    {
        "type": "function",
        "function": {
            "name": "recall_past_conversation",
            "description": "Searches for specific facts, decisions, or context in OLDER conversation history that is no longer in the immediate chat window. Use this when the user refers to a past discussion you don't fully remember.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The specific fact or context you are trying to remember (e.g., 'the price we discussed yesterday')."}
                },
                "required": ["query"]
            }
        }
    }
]
