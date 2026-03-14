import json
from ..logger import logger
from ..utils import format_sync_chat_payload
from ..remote_services.matterminer_core import MatterMinerCoreClient
from ..config import settings

CLIENT_REQUIRED_FIELDS = ["first_name", "last_name", "client_number", "client_type", "email"]

def _get_auth_required_response(message, response_instruction):
    return {
        "status": "auth_required",
        "auth_type": "matterminer_core",
        "message": message,
        "response_instruction": response_instruction
    }

def _get_authenticated_core_client(token, tenant_id):
    core_client = MatterMinerCoreClient(
        base_url=settings.NODE_REMOTE_SERVICE_URL,
        tenant_id=tenant_id
    )
    core_client.set_auth_token(token)
    return core_client

async def handle_core_ops(func_name, args, services, tenant_id, history):
    """
    Handles operations for the MatterMiner Core remote system.
    """
    if func_name == "authenticate_to_core":
        # DEPRECATED: Handled by Node.js UI. 
        # If the LLM somehow calls this (e.g. from cache), steer it to auth_required.
        return _get_auth_required_response(
            "Authentication must be performed via the login card.",
            "The login tool is deprecated. Display the login card to the user instead."
        )

    elif func_name == "create_contact":
        return await handle_create_contact(args, services, tenant_id, history)
        
    elif func_name in ["create_client_record", "setup_client"]:
        return await handle_create_client(args, services, tenant_id, history)

    elif func_name == "lookup_countries":
        return await handle_lookup_countries(args, services, tenant_id)

    return {"status": "error", "message": f"Core operation '{func_name}' not implemented."}

async def handle_lookup_countries(args, services, tenant_id):
    """
    Handles searching for country information.
    """
    # 1. Fetch existing session to get the token
    session = await services['calendar'].get_client_session(tenant_id)
    metadata = session.get("metadata", {})
    token = metadata.get("remote_access_token")
    
    if not token:
        return _get_auth_required_response(
            "Authentication required to lookup countries.",
            "Inform the user that you need them to login to MatterMiner to fetch the country list. Display the login card."
        )
        
    # 2. Call the Remote Service
    core_client = _get_authenticated_core_client(token, tenant_id)
    
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
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to retrieve countries."),
                "response_instruction": "Inform the user that the search failed and ask them to try a different keyword."
            }
    finally:
        await core_client.close()

async def handle_create_contact(args, services, tenant_id, history):
    """
    Handles conversational contact creation with drafting.
    """
    # 1. Fetch existing session context
    session = await services['calendar'].get_client_session(tenant_id)
    metadata = session.get("metadata", {})
    draft = metadata.get("contact_draft", {})
    
    # 2. Update draft with new args (preserving what we already have)
    for key in ["contact_type", "title", "first_name", "middle_name", "last_name", 
                "email", "country_code", "phone_number", "model_type", "model_id", 
                "active", "featured", "country_id"]:
        if args.get(key) is not None:
            draft[key] = args[key]
            
    # Always set defaults if not present
    draft.setdefault("contact_type", "primary")
    draft.setdefault("active", True)
    draft.setdefault("featured", False)
    
    metadata["contact_draft"] = draft
    metadata["active_workflow"] = "contact"
    
    # 3. Gating Logic: Check for mandatory fields
    missing = []
    if not draft.get("first_name"): missing.append("First Name")
    if not draft.get("last_name"): missing.append("Last Name")
    if not draft.get("email"): missing.append("Email Address")
    
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
        
        return {
            "status": "partial_success",
            "message": f"Captured {', '.join([k.replace('_', ' ').title() for k in args.keys() if k in draft])}.",
            "response_instruction": f"Acknowledge the info received. Then, politely ask for the {missing[0]}."
        }
        
    # 5. Scenario B: Ready to commit - Check Authentication
    token = metadata.get("remote_access_token")
    if not token:
        # Save progress but stop for auth
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
            "Tell the user that you have all the contact details ready, but they need to login to MatterMiner first. Display the login card."
        )
        
    # 6. Final Execution: POST to remote API
    core_client = _get_authenticated_core_client(token, tenant_id)
    
    try:
        resp = await core_client.create_contact(draft)
        
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
        else:
            return {
                "status": "error",
                "message": resp.get("message", "Failed to create contact."),
                "response_instruction": "Inform the user that the remote system rejected the request and provide the reason."
            }
    finally:
        await core_client.close()

async def handle_create_client(args, services, tenant_id, history):
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
    final_args = {
        "first_name": args.get("first_name") or client_draft.get("first_name") or db_data.get("first_name"),
        "last_name": args.get("last_name") or client_draft.get("last_name") or db_data.get("last_name"),
        "client_number": args.get("client_number") or client_draft.get("client_number") or db_data.get("client_number"),
        "client_type": args.get("client_type") or client_draft.get("client_type") or db_data.get("client_type"),
        "email": args.get("email") or client_draft.get("email") or db_data.get("email")
    }
    
    logger.info(f"[{tenant_id}] Recovered State: First={final_args['first_name']}, Last={final_args['last_name']}, Email={final_args['email']}")

    incoming_last_name = args.get("last_name")
    if incoming_last_name and any(char.isdigit() for char in str(incoming_last_name)):
        if incoming_last_name == final_args.get("client_number"):
            logger.warning(f"[GLITCH-GUARD] Blocking ID {incoming_last_name} from being saved as last_name.")
            final_args["last_name"] = client_draft.get("last_name") or db_data.get("last_name")
            if final_args["last_name"] == incoming_last_name:
                 final_args["last_name"] = None 

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
    missing = [f for f in CLIENT_REQUIRED_FIELDS if not final_args.get(f)]

    if not missing:
        # 6. GATING: Check for MatterMiner Core Authentication
        token = db_metadata.get("remote_access_token")
        if not token:
             return _get_auth_required_response(
                "Authentication required for MatterMiner Core.",
                "Acknowledge the info received. Tell the user you have all the details, but they need to login to MatterMiner to complete the registration. Display the login card."
            )

        core_client = _get_authenticated_core_client(token, tenant_id)
        try:
            save_result = await core_client.create_client(final_args)
            
            is_truly_saved = False
            if hasattr(save_result, 'status_code'):
                 is_truly_saved = save_result.status_code in [200, 201]
            elif isinstance(save_result, dict):
                 is_truly_saved = save_result.get("status") == "success"

            if is_truly_saved:
                try:
                    wipe_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=db_data,
                        client_draft={
                            "first_name": None,
                            "last_name": None,
                            "client_number": None,
                            "client_type": None,
                            "email": None
                        },
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
            else:
                error_msg = save_result.get("message", "Unknown error")
                return {"status": "error", "message": f"The remote system rejected the record. Reason: {error_msg}"}

            summary_table = (
                "### FINAL SUMMARY: CLIENT REGISTERED\n\n"
                "| Field | Value |\n"
                "| :--- | :--- |\n"
                f"| **First Name** | {final_args.get('first_name')} |\n"
                f"| **Last Name** | {final_args.get('last_name')} |\n"
                f"| **Customer Number** | {final_args.get('client_number', 'N/A')} |\n"
                f"| **Type** | {final_args.get('client_type', 'N/A')} |\n"
                f"| **Email** | {final_args.get('email', 'N/A')} |\n"
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
        captured = [f.replace('_', ' ').title() for f in CLIENT_REQUIRED_FIELDS if final_args.get(f)]
        missing_labels = [f.replace('_', ' ').title() for f in missing]
        next_field = missing[0]
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
        if not contact_draft or not any(v is not None for v in contact_draft.values()):
            return None

        # Filter sensitive keys
        sensitive_keys = ["password", "token", "access_token"]
        clean_contact = {k: v for k, v in contact_draft.items() if v is not None and k not in sensitive_keys}

        if not clean_contact:
            return None

        required_contact = ["first_name", "last_name", "email"]
        missing_contact = [f.replace('_', ' ').title() for f in required_contact if not clean_contact.get(f)]

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
        recov_data = {f: full_state.get(f) for f in CLIENT_REQUIRED_FIELDS if full_state.get(f)}

        if not recov_data:
            return None

        missing = [f.replace('_', ' ').title() for f in CLIENT_REQUIRED_FIELDS if not recov_data.get(f)]
        
        if not missing:
            return None

        return {
            "header": "### RECOVERY MODE: CLIENT INTAKE DETECTED ###",
            "data": recov_data,
            "instruction": f"The user was previously registering a client. Known: {list(recov_data.keys())}. Acknowledge the partial info and ask for the {missing[0]}."
        }

    return None
