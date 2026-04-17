import copy
import json
import asyncio
from ..logger import logger
from ..utils import format_sync_chat_payload, standardize_response, deep_merge_drafts
from ..remote_services.matterminer_core import MatterMinerCoreClient
from ..config import settings

from ..dynamic_schema.client_schema import CLIENT_SCHEMA
from ..dynamic_schema.contact_schema import CONTACT_SCHEMA
from ..dynamic_schema.matter_schema import MATTER_SCHEMA
from ..dynamic_schema.event_schema import STANDARD_EVENT_SCHEMA, ALL_DAY_EVENT_SCHEMA, EVENT_SCHEMA
from ..config import settings

def _get_api_key_error_response(message, response_instruction, history=None):
    """Phase 4 (Auth Migration): Returns a standardized API key error response.
    Used when the Core API rejects the static API key (401/403)."""
    return standardize_response({
        "status": "api_key_error",
        "auth_type": "matterminer_core",
        "message": message,
        "response_instruction": response_instruction
    }, history)

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
    intro_message=None,
    db_session=None,
    user_email=None
):
    """
    Unified engine for conversational drafting.
    Handles field-by-field questioning, optional skipping, and contextual auto-detection.
    """
    # 1. Fetch Session (Atomic State Tunneling)
    session = db_session if db_session is not None else await services['calendar'].get_client_session(tenant_id, user_email=user_email)
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}
    
    # [PHASE A: MULTI-USER ISOLATION]
    # Verify the owner of this session to prevent tenant-wide overlap.
    owner = metadata.get("owner_email")
    if owner and user_email and owner != user_email:
        logger.warning(f"[{tenant_id}] ISOLATION BREACH ATTEMPT: User {user_email} tried to access session of {owner}. Resetting local reference.")
        # If identity mismatch, we treat it as a fresh session for the new user
        metadata = {"owner_email": user_email}
        session["metadata"] = metadata
    elif not owner and user_email:
        metadata["owner_email"] = user_email
    
    # 2. Namespace Isolation: Enforce strict demarcation between workflows
    current_wf = metadata.get("active_workflow")
    if current_wf and current_wf != workflow_id:
        logger.info(f"[{tenant_id}] ISOLATION GUARD: Switching context from {current_wf} to {workflow_id}. Archiving stale {metadata_key}.")
        metadata["active_workflow"] = workflow_id
    
    # 3. State-First Merging: Load existing draft and merge with NEW args only
    # [PHASE B: HARDENED ADDITIVE MERGE]
    # Uses deep_merge_drafts to prevent 'Amnesia' and repetitive questioning.
    vault_draft = metadata.get(metadata_key, {})
    if not isinstance(vault_draft, dict): vault_draft = {}

    # Alias Resolution: Pre-process args to map aliases to primary keys
    resolved_args = {}
    for field in schema:
        key = field["key"]
        for ck in [key] + field.get("aliases", []):
            if ck in args and args[ck] is not None:
                resolved_args[key] = args[ck]
                break

    # --- ANTI-HALLUCINATION PURGE ---
    # Identify the next missing required field before merging
    visible_schema = [f for f in schema if not f.get("system_only", False)]
    missing_before = [f for f in visible_schema if f.get("required", True) and (f["key"] not in vault_draft or not str(vault_draft[f["key"]]).strip() or str(vault_draft[f["key"]]).strip().lower() in ["skip", "skipped", "none", "n/a", "null"])]
    if missing_before:
        expected_key = missing_before[0]["key"]
        
        # We allow updates to already-filled keys AND the strictly expected key.
        # Any other novel key is a hallucination.
        # Check against user's raw text to allow legitimate multi-field answers.
        latest_user_msg = (history[-1]["content"] if history and isinstance(history[-1], dict) and history[-1].get("role") == "user" else "")
        latest_user_text = latest_user_msg.lower() if isinstance(latest_user_msg, str) else ""

        keys_to_remove = []
        for k, v in resolved_args.items():
            is_new_field = k not in vault_draft and k != expected_key
            if is_new_field:
                val_str = str(v).lower().strip()
                # If hallucinated value is not in text anywhere
                if val_str and val_str not in latest_user_text:
                    logger.warning(f"[{tenant_id}] HALLUCINATION PURGE: '{k}'='{v}' was not requested and is not in user text.")
                    keys_to_remove.append(k)
        
        for k in keys_to_remove:
            del resolved_args[k]
    # ----------------------------------
    
    # Apply Hardened Merge with Schema-aware Choice Validation
    draft = deep_merge_drafts(vault_draft, resolved_args, schema=schema)

    # 5. Atomic Sync: Persist the updated draft immediately (Latency Guard)
    metadata[metadata_key] = draft
    metadata["active_workflow"] = workflow_id
    metadata["session_lifecycle"] = "active" # Tracks continuation status
    
    # Verify we aren't losing existing state during the update
    session["metadata"] = metadata
    
    # 6. Gating Logic: Identify next missing required field
    visible_schema = [f for f in schema if not f.get("system_only", False)]
    required_missing = [f for f in visible_schema if f.get("required", True) and (f["key"] not in draft or not str(draft[f["key"]]).strip() or str(draft[f["key"]]).strip().lower() in ["skip", "skipped", "none", "n/a", "null"])]
    optional_missing = [f for f in visible_schema if not f.get("required", True) and f["key"] not in draft]

    # Case A: Still missing required info -> MUST ASK
    if required_missing:
        next_field = required_missing[0]
        captured_labels = [f['label'] for f in visible_schema if f['key'] in draft]
        if captured_labels:
            msg = f"Captured {', '.join(captured_labels)}. To finish, I'll need the **{next_field['label']}**."
        else:
            msg = intro_message if intro_message else f"To start, I'll need the **{next_field['label']}**."
        
        # Enforce Stepwise Instruction to the AI
        instruction = f"The user is in the {workflow_id} workflow. You MUST ask for the {next_field['label']} next. When the user provides this information, you MUST call the relevant tool to update the draft. Do not hallucinate other fields.\n"
        
        if next_field.get("choices"):
            instruction += f"ALLOWED CHOICES: {', '.join(next_field['choices'])}\n"
            msg += f" (Options: {', '.join(next_field['choices'])})"
        
        if not next_field.get("required", True):
            instruction += "This field is OPTIONAL. Tell the user they can say 'skip' to bypass it."
            msg += " (You can say 'skip' to bypass this)."

        return {
            "status": "partial_success",
            "message": msg,
            "response_instruction": instruction
        }, session, draft
    else:
        # ALL REQUIRED ARE MET -> Trigger Submission Case
        return None, session, draft

async def handle_core_ops(func_name, args, services, tenant_id, history, user_email=None, db_session=None):
    """
    Handles operations for the MatterMiner Core remote system.
    Supports session tunneling (db_session) to minimize backend lookups.
    
    Phase 3 (Auth Migration): authenticate_to_core handler REMOVED.
    Core auth is now handled via static API key at the transport layer.
    """
    if func_name == "create_contact":
        return await handle_create_contact(args, services, tenant_id, history, user_email=user_email, db_session=db_session)
        
    elif func_name in ["create_client_record", "setup_client", "promote_contact_to_client"]:
        return await handle_create_client(args, services, tenant_id, history, user_email=user_email, db_session=db_session)

    elif func_name == "search_contact_by_email":
        return await handle_search_contact(args, services, tenant_id, user_email=user_email)

    elif func_name == "lookup_countries":
        return await handle_lookup_countries(args, services, tenant_id, user_email=user_email, db_session=db_session)

    elif func_name == "create_standard_event":
        args["is_all_day"] = False
        return await handle_create_event(args, services, tenant_id, history, user_email=user_email, db_session=db_session)

    elif func_name == "create_all_day_event":
        args["is_all_day"] = True
        return await handle_create_event(args, services, tenant_id, history, user_email=user_email, db_session=db_session)

    elif func_name == "create_matter":
        return await handle_create_matter(args, services, tenant_id, history, user_email=user_email, db_session=db_session)

    elif func_name == "lookup_client":
        return await handle_lookup_client(args, services, tenant_id, user_email=user_email)

    elif func_name == "lookup_practice_area":
        return await handle_lookup_practice_area(args, services, tenant_id, user_email=user_email)

    elif func_name == "lookup_case_stage":
        return await handle_lookup_case_stage(args, services, tenant_id, user_email=user_email)

    elif func_name == "lookup_billing_type":
        return await handle_lookup_billing_type(args, services, tenant_id, user_email=user_email)

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
                    session = await services['calendar'].get_client_session(tenant_id, user_email=user_email)
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

async def handle_lookup_countries(args, services, tenant_id, user_email=None, db_session=None):
    """
    Handles searching for country information.
    Supports session tunneling (db_session) to minimize backend lookups.
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
                    # Tunneling: Reuse fetched session to prevent redundant backend IO
                    session = db_session if db_session is not None else await services['calendar'].get_client_session(tenant_id, user_email=user_email)
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
        elif resp.get("status") == "api_key_error":
            return _get_api_key_error_response(
                "MatterMiner Core rejected the API key. Please contact your administrator.",
                "Inform the user there is a system configuration issue and they should contact their administrator. Do not ask for credentials."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to retrieve countries."),
                "response_instruction": "Inform the user that the search failed and ask them to try a different keyword."
            }
    finally:
        await core_client.close()

async def handle_create_event(args, services, tenant_id, history, user_email=None, db_session=None):
    """
    Handles conversational drafting and final submission of an event to MatterMiner Core.
    """
    is_all_day = args.get("is_all_day", False)
    schema = ALL_DAY_EVENT_SCHEMA if is_all_day else STANDARD_EVENT_SCHEMA
    workflow_id = "all_day_event" if is_all_day else "standard_event"
    metadata_key = "event_draft"
    
    # 1. Start Workflow Engine (Tunneling session to prevent redundant IO)
    partial_resp, session, draft = await run_draft_workflow(
        schema, args, services, tenant_id, metadata_key, workflow_id, history,
        intro_message=f"I'll help you schedule that {'Standard' if not is_all_day else 'All-Day'} event. To start, what should the **{schema[0]['label']}** be?",
        db_session=db_session,
        user_email=user_email
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
        elif resp.get("status") == "api_key_error":
            return _get_api_key_error_response(
                "MatterMiner Core rejected the API key. Please contact your administrator.",
                "Inform the user there is a system configuration issue. All event details are preserved in the vault. Do not ask for credentials."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to create event."),
                "response_instruction": "Inform the user about the rejection and ask to try again or modify."
            }
    finally:
        await core_client.close()

async def handle_create_contact(args, services, tenant_id, history, user_email=None, db_session=None):
    """
    Handles conversational contact creation with drafting.
    """
    # 1. Start Workflow Engine
    partial_resp, session, draft = await run_draft_workflow(
        CONTACT_SCHEMA, args, services, tenant_id, "contact_draft", "contact", history,
        intro_message=f"I'll help you create that contact. To start, what is the **{CONTACT_SCHEMA[0]['label']}**?",
        db_session=db_session,
        user_email=user_email
    )
    
    if partial_resp:
        # PERFORMANCE: Sync the draft back to the database immediately to prevent 'Short-term Memory Loss'
        sync_payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=session,
            contact_draft=draft,
            metadata=session.get("metadata", {}),
            history=[],
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(sync_payload)
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
            
            # --- ADDITION: Summarized Table ---
            summary_rows = "\n".join([f"| **{f.get('label', f['key'])}** | {draft.get(f['key'], 'N/A')} |" for f in CONTACT_SCHEMA if not f.get("system_only")])
            summary_table = (
                "\n\n### FINAL SUMMARY: CONTACT CREATED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"{summary_rows}"
            )
            msg += summary_table
            
            return {
                "status": "success",
                "message": msg,
                "response_instruction": "Confirm the contact has been saved. If the user's goal was client registration, you now have the contact_id and can proceed with that workflow."
            }
        elif resp.get("status") == "api_key_error":
            # Save progress so it can be resumed after admin fixes the API key
            payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=session,
                contact_draft=draft,
                metadata=metadata,
                history=[],
                thread_id=services['calendar'].thread_id
            )
            await services['calendar'].sync_client_session(payload)
            return _get_api_key_error_response(
                "MatterMiner Core rejected the API key. Please contact your administrator.",
                "Inform the user there is a system configuration issue. All contact details are preserved and will be saved once the issue is resolved. Do not ask for credentials."
            )
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to create contact."),
                "response_instruction": "Inform the user that the remote system rejected the request and provide the reason."
            }
    finally:
        await core_client.close()

async def handle_create_client(args, services, tenant_id, history, user_email=None, db_session=None):
    """
    Handles all logic related to client record creation and sequential conversation intake.
    """
    # 1. Start Workflow Engine
    partial_resp, session, draft = await run_draft_workflow(
        CLIENT_SCHEMA, args, services, tenant_id, "client_draft", "client", history,
        intro_message=f"I'll help you register that new client. To start, what is their **{CLIENT_SCHEMA[0]['label']}**?",
        db_session=db_session,
        user_email=user_email
    )
    
    # CASE A: Still gathering data
    if partial_resp:
        # PERFORMANCE: Sync the draft back to the database immediately to prevent 'Short-term Memory Loss'
        sync_payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=session,
            client_draft=draft,
            metadata=session.get("metadata", {}),
            history=[],
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(sync_payload)
        
        # EARLIER LOOKUP PATTERN...
        
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
                    
                    metadata = session.get("metadata", {})
                    if isinstance(metadata, str): metadata = json.loads(metadata)
                    
                    # --- CROSS-POLLINATION: Save the names into a new contact_draft ---
                    # Use existing rehydrated vault_draft or current args
                    metadata["contact_draft"] = {
                        "first_name": args.get("first_name") or draft.get("first_name"),
                        "last_name": args.get("last_name") or draft.get("last_name"),
                        "client_email": email
                    }
                    metadata["_must_create_contact"] = True
                    metadata["client_draft"] = {}
                    metadata["active_workflow"] = None
                    
                    sync_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=session,
                        client_draft={},
                        metadata=metadata,
                        history=[],
                        thread_id=services['calendar'].thread_id,
                        active_workflow="cleared"
                    )
                    await services['calendar'].sync_client_session(sync_payload)
                    await services['calendar'].clear_client_session(tenant_id)
                    
                    return {
                        "status": "partial_success",
                        "next_target": "contact_id",
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
        elif resp.get("status") == "api_key_error":
            return _get_api_key_error_response(
                "MatterMiner Core rejected the API key. Please contact your administrator.",
                "Inform the user there is a system configuration issue. Progress is saved in the vault. Do not ask for credentials."
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
    elif active_workflow == "matter":
        matter_draft = metadata.get("matter_draft", {})
        
        # Merge draft and top-level identity for a complete recovery view
        full_state = {**db_data, **{k:v for k,v in matter_draft.items() if v}}
        
        # Filter to only relevant fields
        recov_data = {f["key"]: full_state.get(f["key"]) for f in MATTER_SCHEMA if full_state.get(f["key"])}

        if not recov_data:
            return None

        missing = [f["label"] for f in MATTER_SCHEMA if f.get("required") and not recov_data.get(f["key"])]
        
        if not missing:
            return None

        return {
            "header": "### RECOVERY MODE: MATTER INTAKE DETECTED ###",
            "data": recov_data,
            "instruction": f"The user was previously creating a matter. Known: {list(recov_data.keys())}. Acknowledge the partial info and ask for the {missing[0]}."
        }

    return None

async def handle_create_matter(args, services, tenant_id, history, user_email=None, db_session=None):
    """
    Handles conversational matter creation with lazy dynamic choice fetching.
    """
    # 1. Fetch current session to calculate dynamic state (Tunneling enabled)
    session = db_session if db_session is not None else await services['calendar'].get_client_session(tenant_id, user_email=user_email)
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}
        
    # Start fresh if switching workflows
    if metadata.get("active_workflow") != "matter":
        metadata["matter_draft"] = {}
        metadata["active_workflow"] = "matter"
        
    # 2. Dynamic Schema population (Scalable & Optimized)
    dynamic_schema = copy.deepcopy(MATTER_SCHEMA)
    draft = metadata.get("matter_draft", {})
    
    # Find next field to see if it needs a pre-fetch
    visible_schema = [f for f in dynamic_schema if not f.get("system_only")]
    missing = [f for f in visible_schema if f["key"] not in draft or not str(draft[f["key"]]).strip()]
    
    if missing:
        field = missing[0]
        if field.get("is_dynamic"):
            cache_key = f"{field['key']}_choices"
            if not metadata.get(cache_key):
                logger.info(f"[{tenant_id}] Fetching dynamic options for {field['key']}...")
                core_client = _get_core_client(tenant_id, user_email)
                try:
                    resp = None
                    if field['key'] == "practice_area_id":
                        resp = await core_client.lookup_practice_areas(is_search=0)
                    elif field["key"] == "case_stage_id":
                        # GET /case-stage?is_search=0 as per specification
                        resp = await core_client.lookup_case_stages(is_search=0)
                    
                    if resp and resp.get("status") != "error":
                        data = resp.get("data", [])
                        metadata[cache_key] = [d["name"] for d in data if d.get("name")]
                        
                        # --- OPTIMIZED CACHE SYNC ---
                        logger.info(f"[{tenant_id}] Syncing dynamic cache for {field['key']} to session.")
                        sync_payload = format_sync_chat_payload(
                            tenant_id=tenant_id,
                            client_args=session,
                            metadata=metadata,
                            history=[],
                            thread_id=services['calendar'].thread_id
                        )
                        await services['calendar'].sync_client_session(sync_payload)
                finally:
                    await core_client.close()
            
            # Inject cached choices into the current field for run_draft_workflow
            field["choices"] = metadata.get(cache_key, [])

    # 3. Hand over to unified engine
    partial_resp, session, draft = await run_draft_workflow(
        dynamic_schema, args, services, tenant_id, "matter_draft", "matter", history,
        intro_message=f"I'll help you create a new matter. To begin, what should we call the **{dynamic_schema[0]['label']}**?",
        db_session=db_session,
        user_email=user_email
    )
    
    if partial_resp:
        # PERFORMANCE: Sync the draft back to the database immediately to prevent 'Short-term Memory Loss'
        sync_payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=session,
            matter_draft=draft,
            metadata=session.get("metadata", {}),
            history=[],
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(sync_payload)
        return partial_resp
        
    metadata = session.get("metadata", {})
    if isinstance(metadata, str):
        try: metadata = json.loads(metadata)
        except: metadata = {}

    core_client = _get_core_client(tenant_id, user_email)
    
    try:
        clean_draft = {k: v for k, v in draft.items() if str(v).lower().strip() not in ["skip", "skipped", "none", "n/a", ""]}
        
        resp = await core_client.create_matter(clean_draft)
        is_success = resp.get("status") == "success" or resp.get("success") is True
        
        if is_success:
            metadata["matter_draft"] = {}
            metadata["active_workflow"] = None
            
            payload = format_sync_chat_payload(
                tenant_id=tenant_id,
                client_args=session,
                matter_draft={},
                metadata=metadata,
                history=[],
                thread_id=services['calendar'].thread_id,
                active_workflow="cleared",
                session_lifecycle="completed"
            )
            await services['calendar'].sync_client_session(payload)
            await services['calendar'].clear_client_session(tenant_id)
            
            summary_rows = "\n".join([f"| **{f.get('label', f['key']).title()}** | {clean_draft.get(f['key'], 'N/A')} |" for f in MATTER_SCHEMA if not f.get("system_only")])
            summary_table = (
                "### FINAL SUMMARY: MATTER CREATED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"{summary_rows}"
            )
            
            return {
                "status": "success",
                "message": f"Successfully created matter: {clean_draft.get('title')}\n\n{summary_table}",
                "data": resp,
                "response_instruction": "Confirm the matter creation success, display the markdown table, and ask if any further steps are needed."
            }
        elif resp.get("status") == "api_key_error":
            payload = format_sync_chat_payload(tenant_id=tenant_id, client_args=session, matter_draft=draft, metadata=metadata, history=[], thread_id=services['calendar'].thread_id)
            await services['calendar'].sync_client_session(payload)
            return _get_api_key_error_response("MatterMiner Core rejected the API key. Please contact your administrator.", "Inform the user there is a system configuration issue. Matter draft is preserved. Do not ask for credentials.")
        else:
            return {"status": "error", "message": resp.get("message", "Failed to create matter."), "response_instruction": "Inform user about the error."}
    finally:
        await core_client.close()

async def handle_lookup_client(args, services, tenant_id, user_email=None):
    term = args.get("search_term", "")
    core_client = _get_core_client(tenant_id, user_email)
    try:
        resp = await core_client.lookup_clients(term)
        return await _process_lookup_response(resp, "client_id", "matter_draft", tenant_id, services, term, user_email=user_email)
    finally:
        await core_client.close()

async def handle_lookup_practice_area(args, services, tenant_id, user_email=None):
    term = args.get("search_term", "")
    core_client = _get_core_client(tenant_id, user_email)
    try:
        # Use is_search=1 as per user spec for retrieving specific ID
        resp = await core_client.lookup_practice_areas(term, is_search=1)
        return await _process_lookup_response(resp, "practice_area_id", "matter_draft", tenant_id, services, term, user_email=user_email)
    finally:
        await core_client.close()

async def handle_lookup_case_stage(args, services, tenant_id, user_email=None):
    term = args.get("search_term", "")
    core_client = _get_core_client(tenant_id, user_email)
    try:
        # User specified that case_stage_id is retrieved from /billing-info?is_search=1
        resp = await core_client.lookup_billing_info(term)
        return await _process_lookup_response(resp, "case_stage_id", "matter_draft", tenant_id, services, term, user_email=user_email)
    finally:
        await core_client.close()

async def handle_lookup_billing_type(args, services, tenant_id, user_email=None):
    term = args.get("search_term", "")
    core_client = _get_core_client(tenant_id, user_email)
    try:
        resp = await core_client.lookup_billing_types(term)
        return await _process_lookup_response(resp, "billing_type_id", "matter_draft", tenant_id, services, term, user_email=user_email)
    finally:
        await core_client.close()

async def _process_lookup_response(resp, link_key, draft_key, tenant_id, services, term, user_email=None):
    is_success = resp.get("status") == "success" or resp.get("success") is True
    if is_success:
        data = resp.get("data", [])
        
        # Check for direct ID in root (e.g. practice_area_id or case_stage_id)
        linked_id = resp.get(link_key)
        
        # If no direct ID, check if it's a unique match in the data list
        if linked_id is None and data and isinstance(data, list) and len(data) == 1:
            linked_id = data[0].get("id")
        
        # Fallback for direct data object
        if linked_id is None and data and isinstance(data, dict) and "id" in data:
            linked_id = data.get("id")
            
        if linked_id is not None:
            # --- PATTERN: LINKING DISCOVERED DATA ---
            try:
                session = await services['calendar'].get_client_session(tenant_id, user_email=user_email)
                metadata = session.get("metadata", {})
                if isinstance(metadata, str): metadata = json.loads(metadata)
                
                draft = metadata.get(draft_key, {})
                draft[link_key] = linked_id
                metadata[draft_key] = draft
                
                payload_args = {
                    "tenant_id": tenant_id,
                    "client_args": session,
                    "metadata": metadata,
                    "history": [],
                    "thread_id": services['calendar'].thread_id
                }
                payload_args[draft_key] = draft
                sync_payload = format_sync_chat_payload(**payload_args)
                await services['calendar'].sync_client_session(sync_payload)
                logger.info(f"[{tenant_id}] Auto-linked {link_key}: {linked_id}")
            except Exception as e:
                logger.error(f"[MATTER-LINK] Failed to sync {link_key}: {e}")

            return {
                "status": "success",
                "message": f"Successfully resolved '{term}' to ID: {linked_id}.",
                "response_instruction": f"The ID {linked_id} has been automatically resolved and linked for {link_key}. Do not ask the user for it again. Ask for the next field."
            }
            
        elif data and isinstance(data, list) and len(data) > 1:
            options = [f"{d.get('name')} (ID: {d.get('id')})" for d in data]
            return {"status": "partial_success", "message": f"Multiple found: {', '.join(options)}", "response_instruction": "Ask the user to clarify which one."}
        else:
            return {"status": "error", "message": f"No matches found for {term}.", "response_instruction": "Inform the user no exact match was found."}
            
    return {"status": "error", "message": "Failed lookup", "response_instruction": "Lookup failed, report error."}
