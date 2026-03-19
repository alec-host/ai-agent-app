import json
from ..logger import logger
from ..utils import format_sync_chat_payload
from ..remote_services.matterminer_core import MatterMinerCoreClient
from ..config import settings

from ..dynamic_schema.client_schema import CLIENT_SCHEMA
from ..dynamic_schema.contact_schema import CONTACT_SCHEMA
from ..dynamic_schema.event_schema import STANDARD_EVENT_SCHEMA, ALL_DAY_EVENT_SCHEMA, EVENT_SCHEMA
from ..config import settings

def _get_auth_required_response(message, response_instruction):
    return {
        "status": "auth_required",
        "auth_type": "matterminer_core",
        "message": message,
        "response_instruction": response_instruction
    }

def _get_core_client(tenant_id, user_email=None):
    return MatterMinerCoreClient(
        base_url=settings.NODE_REMOTE_SERVICE_URL,
        tenant_id=tenant_id,
        user_email=user_email
    )

async def handle_core_ops(func_name, args, services, tenant_id, history, user_email=None):
    """
    Handles operations for the MatterMiner Core remote system.
    """
    if func_name == "authenticate_to_core":
        email = args.get("email")
        password = args.get("password")
        
        if not email or not password:
            return {"status": "error", "message": "Email and password are required for login."}
            
        core_client = _get_core_client(tenant_id, user_email)
        try:
            result = await core_client.login(email, password)
            
            # Robust success check: JSON boolean, string "true", or status wrapper
            is_success = result.get("success") is True or str(result.get("success")).lower() == "true"
            # If client.request caught a non-200 code, it adds status="error"
            if is_success and result.get("status") != "error":
                logger.info(f"[CORE-AUTH] Login successful for {email}")
                return {
                    "status": "success",
                    "message": f"Successfully authenticated as {email}. You can now proceed with your request.",
                    "data": result
                }
            else:
                msg = result.get("message", "Invalid credentials")
                logger.warning(f"[CORE-AUTH] Login failed for {email}: {msg}")
                return {
                    "status": "error",
                    "code": result.get("code", 401),
                    "message": f"Authentication failed: {result.get('message', 'Invalid credentials')}"
                }
        finally:
            await core_client.close()

    elif func_name == "create_contact":
        return await handle_create_contact(args, services, tenant_id, history, user_email=user_email)
        
    elif func_name in ["create_client_record", "setup_client"]:
        return await handle_create_client(args, services, tenant_id, history, user_email=user_email)

    elif func_name == "lookup_countries":
        return await handle_lookup_countries(args, services, tenant_id, user_email=user_email)

    elif func_name == "create_standard_event":
        args["is_all_day"] = False
        return await handle_create_event(args, services, tenant_id, history, user_email=user_email)

    elif func_name == "create_all_day_event":
        args["is_all_day"] = True
        return await handle_create_event(args, services, tenant_id, history, user_email=user_email)

    return {"status": "error", "message": f"Core operation '{func_name}' not implemented."}

async def handle_lookup_countries(args, services, tenant_id, user_email=None):
    """
    Handles searching for country information.
    """
    # 1. Initialize Client
    core_client = _get_core_client(tenant_id, user_email)
    
    try:
        search = args.get("search", "")
        page = args.get("page", 1)
        per_page = args.get("per_page", 15)
        
        resp = await core_client.get_countries(search=search, page=page, per_page=per_page)
        
        if resp.get("status") == "success":
            countries_data = resp.get("data", [])
            formatted_list = []
            for c in countries_data:
                name = c.get("name")
                cid = c.get("id")
                formatted_list.append(f"{name} (ID: {cid})")
                
            return {
                "status": "success",
                "message": f"Found {len(formatted_list)} matches.",
                "countries": formatted_list,
                "raw_data": countries_data,
                "response_instruction": "Display the results to the user. If they have selected one, remember the ID to use as 'country_id' in client/contact creation."
            }
        elif resp.get("status") == "auth_required":
            return _get_auth_required_response(
                "Authentication required for MatterMiner Core.",
                "To search for countries, you need to be logged into MatterMiner. Display the login card."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to retrieve countries."),
                "response_instruction": "Inform the user that the search failed and ask them to try a different keyword."
            }
    finally:
        await core_client.close()

async def handle_create_event(args, services, tenant_id, history, user_email=None):
    """
    Handles conversational drafting and final submission of an event to MatterMiner Core.
    """
    is_all_day = args.get("is_all_day", False)
    schema = ALL_DAY_EVENT_SCHEMA if is_all_day else STANDARD_EVENT_SCHEMA
    workflow_key = "all_day_event" if is_all_day else "standard_event"

    # 1. Fetch Session
    session = await services['calendar'].get_client_session(tenant_id)
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}
    
    draft = metadata.get("event_draft", {})
    
    # 2. Update Draft
    for field in schema:
        key = field["key"]
        val = args.get(key)
        if val is not None and (not isinstance(val, str) or val.strip()):
            draft[key] = val
            
    # Always set workflow context
    metadata["event_draft"] = draft
    metadata["active_workflow"] = workflow_key
    
    # 3. Check for Completion
    # Treat all fields as missing until explicitly provided or skipped
    missing = [f for f in schema if not draft.get(f["key"])]
    
    # Case A: Still Drafting
    if missing:
        payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=session,
            event_draft=draft,
            metadata=metadata,
            history=history,
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(payload)
        
        next_field = missing[0]
        msg = f"Capture received! We have: {', '.join([f['label'] for f in schema if draft.get(f['key'])])}."
        
        instruction = f"Acknowledge the data received. Then, ask ONLY for the {next_field['label']}."
        if not next_field.get("required", True):
            instruction += f" Explicitly tell the user: '(You can say \"skip\" to leave this blank)'."
            
        if next_field['key'] == 'timezone':
            instruction += f"\n\nFor Timezone, instruct the user to pick from common options like: {', '.join([tz['label'] for tz in settings.SUPPORTED_TIMEZONES])}. Use the value like '{settings.SUPPORTED_TIMEZONES[0]['value']}'."

        return {
            "status": "partial_success",
            "message": msg,
            "response_instruction": instruction
        }
        
    # Case B: Ready to Submit
    core_client = _get_core_client(tenant_id, user_email)
    try:
        # Clean out skipped values before submission
        clean_draft = {k: v for k, v in draft.items() if str(v).lower().strip() not in ["skip", "skipped", "none", "n/a", ""]}
        
        # Pass is_all_day flag to handle end_datetime logic if needed
        payload = {**clean_draft, "is_all_day": is_all_day}
        resp = await core_client.create_core_event(payload)
        
        if resp.get("status") == "success" or resp.get("success") is True:
            # Final cleanup
            metadata["event_draft"] = {}
            metadata["active_workflow"] = None
            
            sync_payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=session,
                active_workflow="cleared",
                session_lifecycle="completed",
                event_draft={},
                metadata=metadata,
                history=history,
                thread_id=services['calendar'].thread_id
            )
            await services['calendar'].sync_client_session(sync_payload)
            await services['calendar'].clear_client_session(tenant_id)
            
            summary_rows = "\n".join([f"| **{f.get('label', f['key']).title()}** | {payload.get(f['key'], 'N/A')} |" for f in schema])
            summary_table = (
                "### FINAL SUMMARY: EVENT CREATED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"{summary_rows}"
            )
            
            return {
                "status": "success",
                "message": f"Successfully created your event: {payload.get('title')}\n\n{summary_table}",
                "data": resp,
                "response_instruction": "Confirm success, output the markdown table summary, remind the user it can be copied easily, and ask if they need anything else."
            }
        elif resp.get("status") == "auth_required" or resp.get("code") == 404:
            return _get_auth_required_response(
                "Authentication required for MatterMiner Core.",
                "Display the login card. All event details are preserved in the vault."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to create event."),
                "response_instruction": "Inform the user about the rejection and ask to try again or modify."
            }
    finally:
        await core_client.close()

async def handle_create_contact(args, services, tenant_id, history, user_email=None):
    """
    Handles conversational contact creation with drafting.
    """
    # 1. Fetch existing session context
    session = await services['calendar'].get_client_session(tenant_id)
    metadata = session.get("metadata", {})
    draft = metadata.get("contact_draft", {})
    
    # 2. Update draft with new args (preserving what we already have)
    for field in CONTACT_SCHEMA:
        key = field["key"]
        val = None
        # Cascade search: arg alias -> exact arg
        search_keys = [key] + field.get("aliases", [])
        for k in search_keys:
            candidate = args.get(k)
            # Use candidate if it's a non-empty string or non-None
            if candidate is not None and (not isinstance(candidate, str) or candidate.strip()):
                val = candidate
                break
        
        if val is not None:
            draft[key] = val
            
    # Always set defaults if not present or empty
    for field in CONTACT_SCHEMA:
        if "default" in field:
            current_val = draft.get(field["key"])
            if current_val is None or (isinstance(current_val, str) and not current_val.strip()):
                draft[field["key"]] = field["default"]
    
    metadata["contact_draft"] = draft
    metadata["active_workflow"] = "contact"
    
    # 3. Gating Logic: Check for mandatory fields
    # Treat all non-system fields as missing until explicitly provided or skipped
    missing = [f for f in CONTACT_SCHEMA if not f.get("system_only") and not draft.get(f["key"])]
    
    # 4. Scenario A: Still drafting
    if missing:
        payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=session,
            contact_draft=draft,
            metadata=metadata,
            history=[],
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(payload)
        
        captured_labels = [f['label'] for f in CONTACT_SCHEMA if draft.get(f['key'])]
        msg = f"Captured {', '.join(captured_labels)}." if captured_labels else "Initiated contact drafting."
        
        next_field = missing[0]
        instruction = f"Acknowledge the context. Then, ask ONLY for ONE piece of information: the {next_field['label']}. NEVER ask for multiple fields at once."
        
        if not next_field.get("required", True):
            instruction += f" Explicitly tell the user: '(You can say \"skip\" to use the default or leave it blank)'."
            
        if next_field['key'] == 'country_code':
            # Attempt to pull network_country_code from session/metadata if passed by proxy
            detected_cc = metadata.get("network_country_code") or session.get("network_country_code")
            if detected_cc:
                instruction += f"\n\nHint: The network detected they might be in a region with code '{detected_cc}'. Suggest this code and ask if it's correct for their phone number."
            else:
                instruction += f"\n\nHint: Many users don't know their country dialing code. Do NOT just ask for 'Country Code'. Ask which country their number belongs to (e.g. 'Canada'), then use your `lookup_countries` tool to find the correct dial code automatically."
        
        return {
            "status": "partial_success",
            "message": msg,
            "response_instruction": instruction
        }
        
    # 5. Final Execution: POST to remote API
    core_client = _get_core_client(tenant_id, user_email)
    
    try:
        # Clean out skipped values before submission, but preserve defaults if available
        clean_draft = {}
        for k, v in draft.items():
            if str(v).lower().strip() in ["skip", "skipped", "none", "n/a", ""]:
                schema_field_list = [f for f in CONTACT_SCHEMA if f["key"] == k]
                if schema_field_list and "default" in schema_field_list[0]:
                    clean_draft[k] = schema_field_list[0]["default"]
                continue
            clean_draft[k] = v
            
        resp = await core_client.create_contact(clean_draft)
        
        if resp.get("status") == "success":
            metadata["contact_draft"] = {}
            metadata["active_workflow"] = None
            
            payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=session,
                contact_draft={},
                metadata=metadata,
                history=[],
                thread_id=services['calendar'].thread_id,
                active_workflow="cleared",
                session_lifecycle="completed"
            )
            await services['calendar'].sync_client_session(payload)
            await services['calendar'].clear_client_session(tenant_id)
            return {
                "status": "success",
                "message": f"Contact created successfully: {draft.get('first_name')} {draft.get('last_name')}",
                "response_instruction": "Confirm the contact has been saved to MatterMiner Core and ask if they need anything else."
            }
        elif resp.get("status") == "auth_required":
            # Save progress so user can resume after login
            payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=session,
                contact_draft=draft,
                metadata=metadata,
                history=[],
                thread_id=services['calendar'].thread_id
            )
            await services['calendar'].sync_client_session(payload)
            return _get_auth_required_response(
                "Authentication required for MatterMiner Core.",
                "Display the login card. I have all the details ready to save once you are logged in."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to create contact."),
                "response_instruction": "Inform the user that the remote system rejected the request and provide the reason."
            }
    finally:
        await core_client.close()

async def handle_create_client(args, services, tenant_id, history, user_email=None):
    """
    Handles all logic related to client record creation and sequential conversation intake.
    """
    logger.info(f"[{tenant_id}] Handling Client Creation in Core Agent")

    # 1. FETCH FROM DATABASE (Session Recovery)
    db_data = {}
    db_metadata = {}
    try:
        resp = await services['calendar'].get_client_session(tenant_id)
        db_data = resp if isinstance(resp, dict) else (resp.json() if hasattr(resp, 'json') else {})
        
        discovered_thread_id = db_data.get("threadId")
        if discovered_thread_id:
            services['calendar'].thread_id = discovered_thread_id
            logger.info(f"[{tenant_id}] Client Thread self-discovered: {discovered_thread_id}")

    except Exception as e:
        logger.error(f"[DB-RECOVERY] Failed to fetch session: {e}")

    # 1.5 ROBUST METADATA RECOVERY
    raw_metadata = db_data.get("metadata", {})
    if isinstance(raw_metadata, str):
        try:
            db_metadata = json.loads(raw_metadata)
        except:
            db_metadata = {}
    else:
        db_metadata = raw_metadata or {}

    client_draft = db_metadata.get("client_draft", {})
    db_history = db_metadata.get("chat_history", [])

    # 2. INITIALIZE & SAFE MERGE
    final_args = {}
    for field in CLIENT_SCHEMA:
        key = field["key"]
        val = None
        # Cascade search: arg alias -> exact arg -> draft -> db
        search_keys = [key] + field.get("aliases", [])
        for k in search_keys:
            candidate = args.get(k)
            # Use candidate if it's a non-empty string or non-None
            if candidate is not None and (not isinstance(candidate, str) or candidate.strip()):
                val = candidate
                break
        
        if val is None:
            # Fallback to existing state, matching dynamic schema keys
            val = client_draft.get(key)
            if val is None:
                val = db_data.get(key)
        
        # --- DATA INTEGRITY GUARDS ---
        # 1. Email-as-Type Guard: Prevent email addresses from polluting client_type
        if key == "client_type" and val and "@" in str(val) and "." in str(val):
            logger.warning(f"[STORY-GUARD] Blocking email '{val}' from being saved as client_type.")
            val = client_draft.get("client_type") or db_data.get("client_type")
            
        final_args[key] = val
        
    # Always set defaults if not present or empty
    for field in CLIENT_SCHEMA:
        if "default" in field:
            current_val = final_args.get(field["key"])
            if current_val is None or (isinstance(current_val, str) and not current_val.strip()):
                final_args[field["key"]] = field["default"]
    
    logger.info(f"[{tenant_id}] Recovered State: {final_args}")
    
    # 3. SYNC TO DATABASE
    try:
        sync_payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=db_data,
            client_draft=final_args,
            event_draft=db_metadata.get("event_draft"),
            contact_draft=db_metadata.get("contact_draft"),
            history=history if history else db_history,
            active_workflow="client",
            metadata=db_metadata
        )
        await services['calendar'].sync_client_session(sync_payload)
    except Exception as e:
        logger.error(f"[DB-SYNC] Failed to sync session: {e}", exc_info=True)

    # 5. CHECK FOR COMPLETION
    missing = [f for f in CLIENT_SCHEMA if f.get("required") and not final_args.get(f["key"])]

    if not missing:
        core_client = _get_core_client(tenant_id, user_email)
        try:
            save_result = await core_client.create_client(final_args)
            
            if save_result.get("status") == "success":
                try:
                    wipe_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=db_data,
                        client_draft={f["key"]: None for f in CLIENT_SCHEMA},
                        event_draft=db_metadata.get("event_draft"),
                        contact_draft=db_metadata.get("contact_draft"),
                        active_workflow="cleared", 
                        history=[],
                        session_lifecycle="completed"
                    )
                    await services['calendar'].sync_client_session(wipe_payload)
                    await services['calendar'].clear_client_session(tenant_id)
                except Exception as e:
                    logger.error(f"[CLIENT] Sync wipe failed: {e}")
                
            elif save_result.get("status") == "auth_required":
                return _get_auth_required_response(
                    "Authentication required for MatterMiner Core.",
                    "Almost done! Just login to MatterMiner to complete the registration. Display the login card."
                )
            else:
                error_msg = save_result.get("message", "Unknown error")
                return {"status": "error", "message": f"The remote system rejected the record. Reason: {error_msg}"}

            summary_rows = "\n".join([f"| **{f.get('label', f['key']).title()}** | {final_args.get(f['key'], 'N/A')} |" for f in CLIENT_SCHEMA])
            summary_table = (
                "### FINAL SUMMARY: CLIENT REGISTERED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"{summary_rows}"
            )

            return {
                "status": "success",
                "message": f"### ✅ CLIENT REGISTERED SUCCESSFULLY\n\n{summary_table}\n\n**The session has been cleared.**",
                "data": final_args,
                "_exit_loop": True
            }
        except Exception as e:
            logger.error(f"Final save failed: {e}")
            return {"status": "error", "message": "The system encountered an error while saving the final record."}
        finally:
            await core_client.close()

    else:
        captured = [f["label"] for f in CLIENT_SCHEMA if final_args.get(f["key"])]
        missing_labels = [f["label"] for f in missing]
        next_field = missing[0]["key"]
        next_label = missing_labels[0]

        return {
            "status": "partial_success",
            "current_state": final_args,
            "captured_fields": captured,
            "missing_fields": missing_labels,
            "next_target": next_field,
            "message": f"I've updated the draft. We now have the following details: {', '.join(captured)}. I still need the {next_label}.",
            "response_instruction": (
                f"VAULT SYNCED: You have successfully saved {', '.join(captured)}. "
                f"The NEXT required field is '{next_field}'. "
                f"Acknowledge the info received (briefly) and ask only for the {next_label}. "
                "Do NOT ask for fields you already have."
            )
        }

def get_workflow_recovery(metadata, db_data):
    """
    HOOK: Rehydration logic encapsulated within the Core Agent (for Contact and Client workflows).
    """
    active_workflow = metadata.get("active_workflow")
    lifecycle = metadata.get("session_lifecycle", "active")
    
    if lifecycle == "completed":
        return None

    if active_workflow == "contact":
        contact_draft = metadata.get("contact_draft", {})
        # Filter sensitive keys
        sensitive_keys = ["password", "token", "access_token"]
        clean_contact = {k: v for k, v in contact_draft.items() if v is not None and k not in sensitive_keys}

        if not clean_contact:
            return None

        missing_contact = [f["label"] for f in CONTACT_SCHEMA if not f.get("system_only") and not clean_contact.get(f["key"])]

        recovery = {
            "header": "### PENDING CONTACT RECORD ###",
            "data": clean_contact
        }

        if missing_contact:
            recovery["instruction"] = (
                "### RECOVERY MODE: CONTACT INTAKE DETECTED ###\n"
                f"The user was previously creating a contact. Known: {list(clean_contact.keys())}. "
                f"Acknowledge the partial info and ask for the {missing_contact[0]}."
            )
        return recovery

    elif active_workflow == "client":
        client_draft = metadata.get("client_draft", {})
        
        # Merge draft and top-level identity for a complete recovery view
        full_state = {**db_data, **{k:v for k,v in client_draft.items() if v}}
        
        # Filter to only relevant fields
        recov_data = {f["key"]: full_state.get(f["key"]) for f in CLIENT_SCHEMA if full_state.get(f["key"])}

        if not recov_data:
            return None

        missing = [f["label"] for f in CLIENT_SCHEMA if f.get("required") and not recov_data.get(f["key"])]
        
        if not missing:
            return None

        return {
            "header": "### RECOVERY MODE: CLIENT INTAKE DETECTED ###",
            "data": recov_data,
            "instruction": f"The user was previously registering a client. Known: {list(recov_data.keys())}. Acknowledge the partial info and ask for the {missing[0]}."
        }

    elif active_workflow in ["standard_event", "all_day_event"]:
        event_draft = metadata.get("event_draft", {})
        if not event_draft:
            return None
        
        is_all_day = active_workflow == "all_day_event"
        schema = ALL_DAY_EVENT_SCHEMA if is_all_day else STANDARD_EVENT_SCHEMA
        missing = [f["label"] for f in schema if not event_draft.get(f["key"])]
        
        recovery = {
            "header": "### PENDING EVENT RECORD ###",
            "data": event_draft
        }
        if missing:
            recovery["instruction"] = f"Acknowledge the partial info. Ask ONLY for the {missing[0]}."
        return recovery

    return None
