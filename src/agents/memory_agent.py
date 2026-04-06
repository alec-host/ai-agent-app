import json
from src.logger import logger

async def extract_and_save_facts(tenant_id, history, services, ai_client):
    """
    Background worker to extract persistent facts from the chat history 
    and update the long-term 'Knowledge Vault' in the database.
    """
    if not history or len(history) < 2:
        return

    try:
        # 1. Prepare Extraction Prompt
        # We only look at the last few turns to find new facts
        recent_context = json.dumps(history[-4:], indent=2)
        
        extraction_prompt = f"""
        Analyze the following conversation segment and extract ONLY new, high-certainty persistent facts about the user or their preferences.
        
        Focus on:
        - Job Title / Role
        - Preferred Timezone or Working Hours
        - Specific People or Entities mentioned as recurring contacts
        - Firm-wide policies or specific workflow preferences
        - Personal name (if not already known)

        FORMAT: JSON object with 'facts' key.
        Example: {{"facts": {{"preferred_timezone": "Africa/Nairobi", "role": "Senior Partner"}}}}
        
        If no NEW persistent facts are found, return {{"facts": {{}}}}.
        
        CONVERSATION:
        {recent_context}
        """

        response = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a specialized fact extraction sub-agent."},
                      {"role": "user", "content": extraction_prompt}],
            response_format={"type": "json_object"}
        )

        extracted_data = json.loads(response.choices[0].message.content)
        new_facts = extracted_data.get("facts", {})

        if not new_facts:
            return

        # 2. Update the Vault (Persistence)
        calendar_service = services.get("calendar")
        if not calendar_service:
            return

        db_session = await calendar_service.get_client_session(tenant_id)
        metadata = db_session.get("metadata", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except:
                metadata = {}
        
        global_facts = metadata.get("global_facts", {})
        
        # Merge new facts into existing ones
        updated_facts = {**global_facts, **new_facts}
        metadata["global_facts"] = updated_facts
        
        # Sync back to DB
        await calendar_service.sync_client_session({
            "metadata": metadata
        })
        
        logger.info(f"[MEMORY-AGENT] Successfully extracted and saved {len(new_facts)} new facts for {tenant_id}")

    except Exception as e:
        logger.error(f"[MEMORY-AGENT] Fact extraction failed: {e}", exc_info=True)

def get_memory_recovery(metadata, db_session):
    """
    Retrieves global facts for injection into the system prompt.
    Used by agent_manager.get_rehydration_context.
    """
    global_facts = metadata.get("global_facts", {})
    if not global_facts:
        return None
        
    return {
        "header": "### USER KNOWLEDGE (GLOBAL FACTS) ###",
        "data": global_facts,
        "instruction": "These are persistent facts about the user. Do not ask for them again unless the user explicitly corrects them."
    }
