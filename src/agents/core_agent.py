from ..logger import logger
from ..utils import format_sync_chat_payload

async def handle_core_ops(func_name, args, services, tenant_id, history):
    """
    Handles operations for the MatterMiner Core remote system.
    """
    if func_name == "authenticate_to_core":
        email = args.get("email")
        password = args.get("password")
        
        # 1. Initialize the remote service
        from ..remote_services.matterminer_core import MatterMinerCoreClient
        from ..config import settings
        
        core_client = MatterMinerCoreClient(
            base_url=settings.NODE_SERVICE_URL, # Or a separate MATTERMINER_CORE_URL
            tenant_id=tenant_id
        )
        
        try:
            # 2. Perform Login
            login_resp = await core_client.login(email, password)
            
            if login_resp.get("status") == "success":
                # 3. Success -> Save token to Vault for future turns
                token = login_resp.get("token", {}).get("access_token")
                user_data = login_resp.get("data", {})
                
                # Fetch existing session to update metadata
                session = await services['calendar'].get_client_session(tenant_id)
                metadata = session.get("metadata", {})
                
                # Persist the core token so other agents can use it
                metadata["remote_access_token"] = token
                metadata["remote_user_profile"] = user_data
                
                payload = format_sync_chat_payload(
                    tenant_id=tenant_id,
                    client_args=session,
                    metadata=metadata,
                    history=history,
                    thread_id=services['calendar'].thread_id
                )
                await services['calendar'].sync_client_session(payload)
                
                return {
                    "status": "success",
                    "message": f"Successfully authenticated as {user_data.get('full_name')}.",
                    "response_instruction": "Acknowledge the successful login and ask if they would like to view their profile or current matters."
                }
            else:
                return {
                    "status": "error",
                    "message": login_resp.get("message", "Authentication failed."),
                    "response_instruction": "Inform the user that the credentials provided were incorrect and ask them to try again."
                }
        finally:
            await core_client.close()

    elif func_name == "create_contact":
        return await handle_create_contact(args, services, tenant_id, history)
        
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
        return {
            "status": "auth_required",
            "auth_type": "matterminer_core",
            "message": "Authentication required to lookup countries.",
            "response_instruction": "Inform the user that you need them to login to MatterMiner to fetch the country list. Display the login card."
        }
        
    # 2. Call the Remote Service
    from ..remote_services.matterminer_core import MatterMinerCoreClient
    from ..config import settings
    
    core_client = MatterMinerCoreClient(
        base_url=settings.NODE_SERVICE_URL,
        tenant_id=tenant_id
    )
    core_client.set_auth_token(token)
    
    try:
        search = args.get("search", "")
        page = args.get("page", 1)
        per_page = args.get("per_page", 15)
        
        resp = await core_client.get_countries(search=search, page=page, per_page=per_page)
        
        if resp.get("status") == "success":
            countries_data = resp.get("data", [])
            # Format a clean list for the AI
            # Logic: We extracted 'id' and 'name' (identifier etc if needed)
            formatted_list = []
            for c in countries_data:
                # Based on your requirement: "extracting the id, which will be mapped to country_id as integer"
                name = c.get("name")
                cid = c.get("id")
                formatted_list.append(f"{name} (ID: {cid})")
                
            return {
                "status": "success",
                "message": f"Found {len(formatted_list)} matches.",
                "countries": formatted_list,
                "raw_data": countries_data, # Keep raw for exact mapping if needed
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
                "active", "featured"]:
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
            metadata=metadata,
            history=history,
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(payload)
        
        return {
            "status": "partial_success",
            "message": f"Captured {', '.join([k.replace('_', ' ').title() for k in args.keys() if k in draft])}.",
            "response_instruction": f"Acknowledge the info received. Then, politely ask the user for the missing details: {', '.join(missing)}."
        }
        
    # 5. Scenario B: Ready to commit - Check Authentication
    token = metadata.get("remote_access_token")
    if not token:
        # Save progress but stop for auth
        payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=session,
            metadata=metadata,
            history=history,
            thread_id=services['calendar'].thread_id
        )
        await services['calendar'].sync_client_session(payload)
        
        return {
            "status": "auth_required",
            "auth_type": "matterminer_core",
            "message": "Authentication required for MatterMiner Core.",
            "response_instruction": "Tell the user that you have all the contact details ready, but they need to login to MatterMiner first. Display the login card."
        }
        
    # 6. Final Execution: POST to remote API
    from ..remote_services.matterminer_core import MatterMinerCoreClient
    from ..config import settings
    
    core_client = MatterMinerCoreClient(
        base_url=settings.NODE_SERVICE_URL,
        tenant_id=tenant_id
    )
    core_client.set_auth_token(token)
    
    try:
        resp = await core_client.create_contact(draft)
        
        if resp.get("status") == "success":
            # CLEAR SESSION on success
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
