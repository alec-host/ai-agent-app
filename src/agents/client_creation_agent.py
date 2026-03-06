# agent_manager.py

async def handle_client_intake_partial(tenant_id, incoming_args, services):
    """
    Handles the 'Partial Save' logic to prevent conversational loops.
    Note: We pass 'services' in so it can access the calendar API.
    """
    # 1. Fetch current Vault state
    resp = await services['calendar'].get_client_session(tenant_id)
    
    # Safe extraction (using our previous fix)
    existing_data = resp if isinstance(resp, dict) else (resp.json() if hasattr(resp, 'json') else {})
    existing_metadata = existing_data.get('metadata', {})

    # 2. Merge data (Incremental Update)
    updated_metadata = {**existing_metadata, **incoming_args}

    # 3. Immediate Save to DB
    await services['calendar'].sync_client_session(tenant_id, { ... })

    # 4. Return formatted state for the AI
    return {
        "status": "partial_success",
        "current_state": updated_metadata
    }
