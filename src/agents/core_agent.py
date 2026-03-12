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
                    client_data=session,
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

    return {"status": "error", "message": f"Core operation '{func_name}' not implemented."}
