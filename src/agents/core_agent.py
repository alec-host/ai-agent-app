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

async def run_draft_workflow(
    schema, 
    args, 
    services, 
    tenant_id, 
    metadata_key, 
    workflow_id, 
    history,
    intro_message=None
):
    """
    Unified engine for conversational drafting.
    Handles field-by-field questioning, optional skipping, and contextual auto-detection.
    """
    # 1. Fetch Session
    session = await services['calendar'].get_client_session(tenant_id)
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}
    
    # Start fresh if switching workflows (Isolation)
    if metadata.get("active_workflow") != workflow_id:
        logger.info(f"[{tenant_id}] Switching workflow to {workflow_id}. Clearing stale {metadata_key}.")
        # Also clear other drafts to prevent contamination
        metadata["event_draft"] = {}
        metadata["contact_draft"] = {}
        metadata["client_draft"] = {}
        metadata["active_workflow"] = workflow_id
    
    draft = metadata.get(metadata_key, {})
    
    # SYSTEM CONTEXT (for auto-detection)
    sys_ctx = args.get("_system_context", {})
    
    # 2. Update Draft from provided args
    for field in schema:
        key = field["key"]
        
        # Check direct key and aliases
        candidate_keys = [key] + field.get("aliases", [])
        val = None
        for ck in candidate_keys:
            if ck in args and args[ck] is not None:
                val = args[ck]
                break
        
        # Determine if value was explicitly provided
        if val is not None:
            # Handle explicit 'skip' with contextual fallback (e.g. for Timezone)
            is_skipping = str(val).lower().strip() in ["skip", "skipped", "none", "n/a", ""]
            ctx_key = field.get("suggest_from_context")
            
            if is_skipping:
                if ctx_key and sys_ctx.get(ctx_key):
                    draft[key] = sys_ctx.get(ctx_key)
                    logger.info(f"[{tenant_id}] User skipped {key}, auto-detected: {draft[key]}")
                else:
                    # Mark as skipped with a sentinel to satisfy the 'missing' check
                    draft[key] = "skipped"
                    logger.info(f"[{tenant_id}] Field {key} explicitly skipped.")
            # Handle list types (like attendees)
            elif field.get("type") == "list":
                if isinstance(val, list):
                    draft[key] = val
                elif isinstance(val, str):
                    draft[key] = [v.strip() for v in val.split(",") if v.strip()]
            else:
                draft[key] = val

    # TEMPORAL LOGIC: Handle duration -> end_datetime calculation
    if "duration_minutes" in args and draft.get("start_datetime"):
        try:
            from datetime import datetime, timedelta
            start_str = draft["start_datetime"]
            # Flexible parse
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
                try:
                    start_dt = datetime.strptime(start_str.split(".")[0], fmt)
                    duration = int(args["duration_minutes"])
                    draft["end_datetime"] = (start_dt + timedelta(minutes=duration)).isoformat()
                    break
                except: continue
        except Exception as e:
            logger.warning(f"Failed to calculate duration-based end time: {e}")
            
    # 1.5 Sync workflow state back to session object
    metadata[metadata_key] = draft
    metadata["active_workflow"] = workflow_id
    
    # Handle serialization if DB stored it as a string
    if isinstance(session.get("metadata"), str):
        session["metadata"] = json.dumps(metadata)
    else:
        session["metadata"] = metadata
    
    # 3. Check for Completion
    # Use 'key in draft' to allow None/Empty values to count as 'Handled'
    # GATING: Skip fields marked as 'system_only' for user prompts
    visible_schema = [f for f in schema if not f.get("system_only", False)]
    
    missing = [f for f in visible_schema if f["key"] not in draft or not str(draft[f["key"]]).strip()]
    
    # Case A: Still Drafting
    if missing:
        payload_args = {
            "tenant_id": tenant_id,
            "client_args": session,
            "metadata": metadata,
            "history": history,
            "thread_id": services['calendar'].thread_id
        }
        # Add dynamic draft injection to format_sync_chat_payload
        if metadata_key == "event_draft": payload_args["event_draft"] = draft
        elif metadata_key == "contact_draft": payload_args["contact_draft"] = draft
        elif metadata_key == "client_draft": payload_args["client_draft"] = draft
        
        payload = format_sync_chat_payload(**payload_args)
        await services['calendar'].sync_client_session(payload)
        
        next_field = missing[0]
        captured_labels = [f['label'] for f in visible_schema if f['key'] in draft and str(draft[f['key']]).strip()]
        
        # Build Message
        msg_suffix = f" (You can say 'skip' to bypass this)." if not next_field.get("required", True) else ""
        if captured_labels:
            msg = f"Captured {', '.join(captured_labels)}. To finish, I'll need the {next_field['label']}.{msg_suffix}"
        else:
            msg = (intro_message or f"Sure, let's set that up. First, what is the {next_field['label']}?") + msg_suffix
            
        # Build Instruction
        instruction = f"### STRICT GATING: Ask ONLY for the {next_field['label']}.\n"
        
        lookup_tool = next_field.get("lookup_tool")
        if lookup_tool:
            instruction += f"CRITICAL: When the user provides the name, DO NOT attempt to map it directly to `{next_field['key']}`.\n"
            instruction += f"INSTEAD, you MUST call the `{lookup_tool}` tool to search for the correct system ID.\n"
            instruction += f"Once `{lookup_tool}` finds the ID, it will automatically link it to this drafting session. You can then ask for the next field.\n"
        else:
            instruction += "When the user responds, IMMEDIATELY call the tool again and map their input to the '" + next_field['key'] + "' field.\n"
            
        instruction += "DO NOT ask for any other information until this field is provided or skipped.\n"
        instruction += "Ask ONE question for this single field. Avoid grouping questions."
        
        # Handle CHOICE guidance (e.g. for Title/Salutation)
        if next_field.get("choices"):
            instruction += f"\n\nGUIDANCE: Present these as the only suggested options: {', '.join(next_field['choices'])}."
        
        # Handle SKIP option
        if not next_field.get("required", True):
            instruction += f" Explicitly tell the user: '(You can say \"skip\" to leave this blank)'."
            
        # Handle CONTEXTUAL SUGGESTION (e.g. Timezone)
        ctx_key = next_field.get("suggest_from_context")
        if ctx_key and sys_ctx.get(ctx_key):
            val = sys_ctx.get(ctx_key)
            instruction += f"\n\nSystem detected '{val}' for this field. Suggest this as the default if they skip."
            
        if next_field['key'] == 'timezone':
            detected = sys_ctx.get("user_timezone_name", "UTC")
            common_tzs = list(set([detected, "Africa/Nairobi", "UTC", "America/New_York", "Europe/London", "Asia/Dubai"]))
            instruction += f"\n\nProvide these as examples for the user (The first one is their detected local time): {', '.join(common_tzs)}."

        return {
            "status": "partial_success",
            "message": msg,
            "response_instruction": instruction
        }, session, draft
        
    return None, session, draft # Return None if complete; caller handles submission

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
        
    elif func_name in ["create_client_record", "setup_client", "promote_contact_to_client"]:
        return await handle_create_client(args, services, tenant_id, history, user_email=user_email)

    elif func_name == "search_contact_by_email":
        return await handle_search_contact(args, services, tenant_id, user_email=user_email)

    elif func_name == "lookup_countries":
        return await handle_lookup_countries(args, services, tenant_id, user_email=user_email)

    elif func_name == "create_standard_event":
        args["is_all_day"] = False
        return await handle_create_event(args, services, tenant_id, history, user_email=user_email)

    elif func_name == "create_all_day_event":
        args["is_all_day"] = True
        return await handle_create_event(args, services, tenant_id, history, user_email=user_email)

    return {"status": "error", "message": f"Core operation '{func_name}' not implemented."}

async def handle_search_contact(args, services, tenant_id, user_email=None):
    """
    Searches for a contact by email via the backend.
    """
    email = args.get("email")
    if not email:
        return {
            "status": "error",
            "message": "Email address is required to search for a contact.",
            "response_instruction": "Ask the user for the email address to search."
        }

    core_client = _get_core_client(tenant_id, user_email)
    try:
        resp = await core_client.search_contact_by_email(email)
        
        # Robust success check
        is_success = resp.get("status") == "success" or resp.get("success") is True

        if is_success:
            # Robust extraction for search
            contact_id = (
                resp.get("contact_id") or 
                resp.get("data", {}).get("contact_id") or 
                resp.get("data", {}).get("data", {}).get("id") or 
                resp.get("data", {}).get("id")
            )
            
            if contact_id:
                # --- PATTERN: LINKING DISCOVERED DATA ---
                # Attempt to pre-fill client_draft for future client registration
                try:
                    session = await services['calendar'].get_client_session(tenant_id)
                    metadata = session.get("metadata", {})
                    if isinstance(metadata, str): metadata = json.loads(metadata)
                    
                    client_draft = metadata.get("client_draft", {})
                    client_draft["contact_id"] = contact_id
                    metadata["client_draft"] = client_draft
                    
                    sync_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=session,
                        client_draft=client_draft,
                        metadata=metadata,
                        history=[],
                        thread_id=services['calendar'].thread_id
                    )
                    await services['calendar'].sync_client_session(sync_payload)
                except Exception as e:
                    logger.error(f"[SEARCH-LINK] Failed to sync contact_id to client_draft: {e}")

                return {
                    "status": "success",
                    "message": f"Contact found! ID is {contact_id}.",
                    "data": resp,
                    "response_instruction": f"The contact has been discovered (ID: {contact_id}) and linked to the current draft. You can now proceed to create a client record for them if that was the user's intent, or ask what else they need."
                }
            else:
                # 200 OK but no contact_id present (empty result)
                return {
                    "status": "not_found",
                    "message": f"No contact found for {email}.",
                    "response_instruction": "Inform the user that no contact was found with that email. Ask if they would like to create a new contact instead."
                }
                
        # Handle 404 or explicit 'not_found' status mapping to creation
        if resp.get("code") == 404 or resp.get("status") == "not_found":
            return {
                "status": "not_found",
                "message": f"No contact found for {email}.",
                "response_instruction": "Inform the user that no contact was found with that email. Ask if they would like to create a new contact instead."
            }

        return {
            "status": "error",
            "message": resp.get("message", "Unknown error while searching for contact."),
            "response_instruction": "Inform the user that the search failed and ask them to try again later."
        }
    finally:
        await core_client.close()

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
        
        # Robust success check for either status:success or success:true
        is_success = resp.get("status") == "success" or resp.get("success") is True
        
        if is_success:
            # 2.A: Check for Direct ID Reward Pattern (The nodejs payload below)
            # { "success": true, "country_id": 15, "message": "Retreived country id successfully" }
            direct_id = resp.get("country_id")
            
            countries_data = resp.get("data", [])
            # If we don't have a list but we have a direct ID, treat it as single result
            if direct_id and not countries_data:
                countries_data = [{"id": direct_id, "name": search or "Identified Country"}]

            formatted_list = []
            for c in countries_data:
                name = c.get("name")
                cid = c.get("id")
                if name and cid:
                    formatted_list.append(f"{name} (ID: {cid})")
                
            # --- PATTERN: LINKING DISCOVERED DATA ---
            # If only one match is found (either via direct ID or list of 1), auto-link it
            linked_id = None
            if direct_id:
                linked_id = direct_id
            elif len(countries_data) == 1:
                linked_id = countries_data[0].get("id")

            if linked_id:
                try:
                    session = await services['calendar'].get_client_session(tenant_id)
                    metadata = session.get("metadata", {})
                    if isinstance(metadata, str): metadata = json.loads(metadata)
                    
                    client_draft = metadata.get("client_draft", {})
                    client_draft["country_id"] = linked_id
                    metadata["client_draft"] = client_draft
                    
                    sync_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=session,
                        client_draft=client_draft,
                        metadata=metadata,
                        history=[],
                        thread_id=services['calendar'].thread_id
                    )
                    await services['calendar'].sync_client_session(sync_payload)
                    logger.info(f"[{tenant_id}] Auto-linked country_id: {linked_id}")
                except Exception as e:
                    logger.error(f"[COUNTRY-LINK] Failed to sync country_id: {e}")

            return {
                "status": "success",
                "message": resp.get("message", f"Found {len(formatted_list)} matches."),
                "countries": formatted_list,
                "country_id": linked_id,
                "raw_data": countries_data,
                "response_instruction": "Display the result to the user. Since the country is identified, its ID has been automatically linked. You can move to the next field."
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
    workflow_id = "all_day_event" if is_all_day else "standard_event"
    metadata_key = "event_draft"
    
    # 1. Start Workflow Engine
    partial_resp, session, draft = await run_draft_workflow(
        schema, args, services, tenant_id, metadata_key, workflow_id, history,
        intro_message=f"I'll help you schedule that {'Standard' if not is_all_day else 'All-Day'} event. To start, what should the **{schema[0]['label']}** be?"
    )
    
    if partial_resp:
        return partial_resp
        
    # Case B: Ready to Submit
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}
    
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
    # 1. Start Workflow Engine
    partial_resp, session, draft = await run_draft_workflow(
        CONTACT_SCHEMA, args, services, tenant_id, "contact_draft", "contact", history,
        intro_message="I'll help you create that contact. To start, what is their **First Name**?"
    )
    
    if partial_resp:
        return partial_resp
        
    # Case B: Ready to Submit
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}
        
    # 5. Final Execution: POST to remote API
    core_client = _get_core_client(tenant_id, user_email)
    
    try:
        # Clean out skipped values before submission
        clean_draft = {k: v for k, v in draft.items() if str(v).lower().strip() not in ["skip", "skipped", "none", "n/a", ""]}
        
        # Pass payload to Core API
        resp = await core_client.create_contact(clean_draft)
        
        # Robust success check
        is_success = resp.get("status") == "success" or resp.get("success") is True
        
        if is_success:
            # --- PATTERN: LINKING FRESH DATA ---
            # Robust Extraction for nested Node.js payloads (data: { data: { id: 53 } })
            contact_id = (
                resp.get("contact_id") or 
                resp.get("data", {}).get("contact_id") or 
                resp.get("data", {}).get("data", {}).get("id") or 
                resp.get("data", {}).get("id")
            )
            
            # Propagation: Use the draft email as source of truth to prevent null wipes
            # if the server returns email: null. 
            success_email = draft.get("client_email") or resp.get("data", {}).get("data", {}).get("email")
            
            metadata["contact_draft"] = {}
            metadata["active_workflow"] = None
            
            # Independent Workflow: Contact creation no longer forcibly seeds client_draft.
            # This ensures that contact management remains a decoupled operation.
            
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
            
            msg = f"Contact created successfully: {draft.get('first_name')} {draft.get('last_name')}"
            if contact_id: msg += f" (ID: {contact_id})"
            
            return {
                "status": "success",
                "message": msg,
                "response_instruction": "Confirm the contact has been saved. If the user's goal was client registration, you now have the contact_id and can proceed with that workflow."
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
    # 1. Start Workflow Engine
    partial_resp, session, draft = await run_draft_workflow(
        CLIENT_SCHEMA, args, services, tenant_id, "client_draft", "client", history,
        intro_message="I'll help you register that new client. To start, what is their **Email Address**?"
    )
    
    # CASE A: Still gathering data
    if partial_resp:
        # EARLIER LOOKUP PATTERN: Intercept the intake as soon as 'client_email' is available.
        email = draft.get("client_email")
        if email and not draft.get("contact_id"):
            logger.info(f"[{tenant_id}] Early lookup for contact_id using email: {email}")
            core_client = _get_core_client(tenant_id, user_email)
            try:
                search_resp = await core_client.search_contact_by_email(email)
                is_success = search_resp.get("status") == "success" or search_resp.get("success") is True
                contact_id = (
                    search_resp.get("contact_id") or 
                    search_resp.get("data", {}).get("contact_id") or 
                    search_resp.get("data", {}).get("data", {}).get("id") or 
                    search_resp.get("data", {}).get("id")
                )
                
                if is_success and contact_id:
                    logger.info(f"[{tenant_id}] Found and linked contact_id: {contact_id}")
                    # Update local draft with the discovered contact_id
                    draft["contact_id"] = contact_id
                    
                    # Update session metadata immediately to reflect progress
                    metadata = session.get("metadata", {})
                    if isinstance(metadata, str): metadata = json.loads(metadata)
                    metadata["client_draft"] = draft
                    
                    sync_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=session,
                        client_draft=draft,
                        metadata=metadata,
                        history=[],
                        thread_id=services['calendar'].thread_id
                    )
                    await services['calendar'].sync_client_session(sync_payload)
                    
                    # We proceed with the original partial_resp (the LLM will ask the next field in the schema)
                else:
                    # BLOCKING FALLBACK: If contact isn't found, the workflow MUST stop.
                    # This enforces independence between creation workflows.
                    # Clear vault and chat session as required.
                    metadata = session.get("metadata", {})
                    if isinstance(metadata, str): metadata = json.loads(metadata)
                    
                    metadata["client_draft"] = {}
                    metadata["active_workflow"] = None
                    
                    sync_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=session,
                        client_draft={},
                        metadata=metadata,
                        history=[],
                        thread_id=services['calendar'].thread_id,
                        active_workflow="cleared",
                        session_lifecycle="completed"
                    )
                    await services['calendar'].sync_client_session(sync_payload)
                    await services['calendar'].clear_client_session(tenant_id)
                    
                    return {
                        "status": "success",
                        "_exit_loop": True,
                        "message": f"I couldn't find a contact for the email address '{email}'. The client registration has been canceled and your session was cleared.",
                        "response_instruction": (
                            "Inform the user that a contact record is required to create a client. "
                            "Explain that no contact was found for this email, so the client creation process was aborted and the session was cleared. "
                            "Ask if they would like to create a contact first using 'create_contact'."
                        )
                    }
            except Exception as e:
                logger.error(f"[EARLY-LOOKUP] Failed: {e}")
            finally:
                await core_client.close()

        return partial_resp
        
    # CASE B: Ready to Submit
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}

    # Ensure thread_id is set
    discovered_thread_id = session.get("threadId")
    if discovered_thread_id:
        services['calendar'].thread_id = discovered_thread_id

    # 3. FINAL SUBMISSION
    core_client = _get_core_client(tenant_id, user_email)
    try:
        # Clean out skipped values before submission
        clean_draft = {k: v for k, v in draft.items() if str(v).lower().strip() not in ["skip", "skipped", "none", "n/a", ""]}
        
        # Pass payload to Core API
        resp = await core_client.create_client(clean_draft)
        
        # Success Check
        is_success = resp.get("status") == "success" or resp.get("success") is True
        
        if is_success:
            # Final cleanup
            metadata["client_draft"] = {}
            metadata["active_workflow"] = None
            
            payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=session,
                client_draft={},
                metadata=metadata,
                history=[],
                thread_id=services['calendar'].thread_id,
                active_workflow="cleared",
                session_lifecycle="completed"
            )
            await services['calendar'].sync_client_session(payload)
            await services['calendar'].clear_client_session(tenant_id)
            
            # Build Summary Table
            summary_rows = "\n".join([f"| **{f.get('label', f['key'])}** | {draft.get(f['key'], 'N/A')} |" for f in CLIENT_SCHEMA if not f.get("system_only")])
            summary_table = (
                "### FINAL SUMMARY: CLIENT REGISTERED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"{summary_rows}"
            )
            
            return {
                "status": "success",
                "message": f"Successfully registered client: {draft.get('first_name')} {draft.get('last_name')}\n\n{summary_table}",
                "data": resp,
                "response_instruction": "Confirm success and ask if they would like to create a matter for this client."
            }
        elif resp.get("status") == "auth_required":
            return _get_auth_required_response(
                "Authentication required for MatterMiner Core.",
                "Display login card. Progress is saved in the vault."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to create client."),
                "response_instruction": "Inform the user about the error and ask to retry."
            }
    finally:
        await core_client.close()

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
