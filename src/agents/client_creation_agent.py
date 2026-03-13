# src/agents/client_creation_agent.py
import json
from src.logger import logger
from src.utils import format_sync_chat_payload
from src.remote_services.matterminer_core import MatterMinerCoreClient
from src.config import settings

# The full list of fields required for a complete client record - ORDERED BY PRIORITY
REQUIRED_FIELDS = ["first_name", "last_name", "client_number", "client_type", "email"]

async def handle_client_creation(func_name, args, services, tenant_id, history):
    """
    Handles all logic related to client record creation and sequential conversation intake.
    """
    logger.info(f"[{tenant_id}] Handling Client Creation: {func_name}")

    # 1. FETCH FROM DATABASE (Session Recovery)
    db_data = {}
    db_metadata = {}
    try:
        resp = await services['calendar'].get_client_session(tenant_id)
        db_data = resp if isinstance(resp, dict) else (resp.json() if hasattr(resp, 'json') else {})
        
        # SELF-DISCOVERY: Read the threadId from the DB record and bind it to the service client.
        # This ensures the client registration stays pinned to the correct row.
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
    # Recover chat history from metadata if available
    db_history = db_metadata.get("chat_history", [])

    # 2. INITIALIZE & SAFE MERGE (Prioritize new args -> then Draft -> then top-level DB)
    # We use a double-fallback to ensure that data in the 'Identity' columns is utilized
    # if the 'Draft' namespace is somehow corrupted or missing.
    final_args = {
        "first_name": args.get("first_name") or client_draft.get("first_name") or db_data.get("first_name"),
        "last_name": args.get("last_name") or client_draft.get("last_name") or db_data.get("last_name"),
        "client_number": args.get("client_number") or client_draft.get("client_number") or db_data.get("client_number"),
        "client_type": args.get("client_type") or client_draft.get("client_type") or db_data.get("client_type"),
        "email": args.get("email") or client_draft.get("email") or db_data.get("email")
    }
    
    # 2.3 RECOVERY LOGGING: Helpful for tracing "Amnesia" glitches
    logger.info(f"[{tenant_id}] Recovered State: First={final_args['first_name']}, Last={final_args['last_name']}, Email={final_args['email']}")

    # 2.5 GLITCH GUARD: Prevent ID format from leaking into last_name
    incoming_last_name = args.get("last_name")
    if incoming_last_name and any(char.isdigit() for char in str(incoming_last_name)):
        # If it matches the client number, it's a mapping error
        if incoming_last_name == final_args.get("client_number"):
            logger.warning(f"[GLITCH-GUARD] Blocking ID {incoming_last_name} from being saved as last_name.")
            # RESTORE FROM PREVIOUS KNOWN: Use db_data or client_draft as fallback
            final_args["last_name"] = client_draft.get("last_name") or db_data.get("last_name")
            if final_args["last_name"] == incoming_last_name: # Still numeric?
                 final_args["last_name"] = None # Wipe it completely to stop the corruption

    # 3. SYNC TO DATABASE (Incremental Persistence)
    try:
        # Debug: Log what we are trying to save
        logger.info(f"[DB-SYNC] Prepared Args: {final_args}")
        
        # Use the unified payload formatter
        sync_payload = format_sync_chat_payload(
            tenant_id=tenant_id,
            client_args=db_data, # Use existing identity
            client_draft=final_args, # Isolated workflow draft
            event_draft=db_metadata.get("event_draft"),
            contact_draft=db_metadata.get("contact_draft"),
            history=history if history else db_history,
            active_workflow="client",
            metadata=db_metadata # Maintain all other keys (tokens, etc)
        )
        
        await services['calendar'].sync_client_session(sync_payload)
        logger.info(f"[DB-SYNC] Success for tenant {tenant_id}. Metadata keys: {list(sync_payload['metadata'].keys())}")
    except Exception as e:
        logger.error(f"[DB-SYNC] Failed to sync session: {e}", exc_info=True)

    # 5. CHECK FOR COMPLETION
    missing = [f for f in REQUIRED_FIELDS if not final_args.get(f)]

    if not missing:
        # 6. GATING: Check for MatterMiner Core Authentication
        token = db_metadata.get("remote_access_token")
        if not token:
             return {
                "status": "auth_required",
                "auth_type": "matterminer_core",
                "message": "Authentication required for MatterMiner Core.",
                "response_instruction": "Acknowledge the info received. Tell the user you have all the details, but they need to login to MatterMiner to complete the registration. Display the login card."
            }

        # ALL FIELDS CAPTURED & AUTHENTICATED: Finalize the record
        try:
            # Separation of Concerns: Use the Core Client for Core Services
            core_client = MatterMinerCoreClient(
                base_url=settings.NODE_REMOTE_SERVICE_URL,
                tenant_id=tenant_id
            )
            core_client.set_auth_token(token)
            
            logger.info(f"[CLIENT] Initiating remote save to MatterMiner Core for tenant {tenant_id}")
            save_result = await core_client.create_client(final_args)
            logger.info(f"Final record save result: {save_result}")
            
            # CLEAR DRAFT SESSION: Important to prevent the AI from seeing "Locked" data on the next new client
            # SECURITY: Only clear if the remote save actually worked (200-201 or specific success code)
            is_truly_saved = False
            if hasattr(save_result, 'status_code'):
                 is_truly_saved = save_result.status_code in [200, 201]
            elif isinstance(save_result, dict):
                 is_truly_saved = save_result.get("status") == "success"

            if is_truly_saved:
                try:
                    wipe_payload = format_sync_chat_payload(
                        tenant_id=tenant_id,
                        client_args=db_data, # Maintain identity if needed, but the session is being cleared anyway
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
                logger.error(f"[CLIENT] Remote save failed. Reason: {error_msg}")
                return {"status": "error", "message": f"The remote system rejected the record. Reason: {error_msg}"}

            # Format the success message with a structured Markdown table for HTML rendering
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

    else:
        # PARTIAL PROGRESS: Lock progress and instruct the AI on exactly what to ask next
        captured = [f.replace('_', ' ').title() for f in REQUIRED_FIELDS if final_args.get(f)]
        missing_labels = [f.replace('_', ' ').title() for f in missing]
        next_field = missing[0]
        next_label = missing_labels[0]

        logger.warning(f"[INTAKE-PROGRESS] Captured: {', '.join(captured)} | Missing: {', '.join(missing_labels)}")

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
    HOOK: Rehydration logic encapsulated within the Client Agent.
    """
    active_workflow = metadata.get("active_workflow")
    lifecycle = metadata.get("session_lifecycle", "active")
    client_draft = metadata.get("client_draft", {})

    if active_workflow != "client" or lifecycle == "completed":
        return None

    # Merge draft and top-level identity for a complete recovery view
    full_state = {**db_data, **{k:v for k,v in client_draft.items() if v}}
    
    # Filter to only relevant fields
    recov_data = {f: full_state.get(f) for f in REQUIRED_FIELDS if full_state.get(f)}

    if not recov_data:
        return None

    missing = [f.replace('_', ' ').title() for f in REQUIRED_FIELDS if not recov_data.get(f)]
    
    if not missing:
        return None

    return {
        "header": "### RECOVERY MODE: CLIENT INTAKE DETECTED ###",
        "data": recov_data,
        "instruction": f"The user was previously registering a client. Known: {list(recov_data.keys())}. Acknowledge the partial info and ask for the {missing[0]}."
    }
