import json
import asyncio
from src.logger import logger
from src.remote_services.pinecone_service import PineconeClient

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
        Analyze the following conversation segment and extract NEW persistent facts OR updates/corrections to existing facts about the user.
        
        Focus on:
        - Job Title / Role
        - Preferred Timezone or Working Hours
        - Specific People or Entities mentioned as recurring contacts
        - Firm-wide policies or specific workflow preferences
        - Personal name (if not already known)

        CRITICAL: If the user explicitly CORRECTS a previously mentioned fact (e.g., "Actually, my title is Partner now" or "Change my timezone to PST"), prioritize this new value.

        FORMAT: JSON object with 'facts' key.
        Example: {{"facts": {{"preferred_timezone": "Africa/Nairobi", "role": "Senior Partner"}}}}
        
        If no NEW persistent facts or CORRECTIONS are found, return {{"facts": {{}}}}.
        
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

        # 3. VECTORIZE AND INDEX TO PINECONE (Cognitive Bridge)
        asyncio.create_task(index_facts_in_pinecone(tenant_id, new_facts, ai_client))

    except Exception as e:
        logger.error(f"[MEMORY-AGENT] Fact extraction failed: {e}", exc_info=True)

async def index_facts_in_pinecone(tenant_id, facts, ai_client):
    """
    Tier 3: Semantics Indexing.
    Encodes persistent facts as vectors and upserts them into a dedicated namespace for the tenant.
    """
    if not facts or not isinstance(facts, dict):
        return

    pinecone = PineconeClient()
    if not pinecone.is_configured:
        return

    try:
        vectors = []
        for key, value in facts.items():
            context_string = f"Fact about the user: {key} is {value}"
            
            # Generate Embedding (Tier 2/3 Encoding)
            resp = await ai_client.embeddings.create(
                model="text-embedding-3-small",
                input=context_string
            )
            embedding = resp.data[0].embedding
            
            vectors.append({
                "id": f"fact_{tenant_id}_{hash(key)}", # Stable ID for idempotency
                "values": embedding,
                "metadata": {
                    "tenant_id": tenant_id,
                    "fact_name": key,
                    "fact_value": str(value),
                    "context": context_string,
                    "type": "persistent_fact"
                }
            })
            
        if vectors:
            await pinecone.upsert_vectors(vectors, namespace=tenant_id)
            logger.info(f"[MEMORY-AGENT] Indexed {len(vectors)} facts into Pinecone for {tenant_id}")

    except Exception as e:
        logger.error(f"[MEMORY-AGENT] Pinecone indexing failed: {e}", exc_info=True)

async def summarize_and_save(tenant_id, history, services, ai_client):
    """
    Tier 2: Incremental Summarization.
    Collapses long conversation history into a concise summary to preserve context
    while staying within token limits.
    """
    # Threshold for summarization (e.g., more than 15 actual turns)
    if not history or len(history) < 15:
        return

    try:
        calendar_service = services.get("calendar")
        db_session = await calendar_service.get_client_session(tenant_id)
        metadata = db_session.get("metadata", {})
        if isinstance(metadata, str):
            try: metadata = json.loads(metadata)
            except: metadata = {}
            
        # Don't summarize if we recently did it
        last_summary_turn = metadata.get("last_summary_turn_count", 0)
        if len(history) - last_summary_turn < 10:
            return

        logger.info(f"[MEMORY-AGENT] Triggering incremental summarization for {tenant_id} (History: {len(history)} turns)")

        # 1. Summarize oldest messages
        old_history = json.dumps(history[:-5], indent=2) # Keep the last 5 turns for immediate context
        
        summary_prompt = f"""
        Summarize the following conversation history into a concise, high-density paragraph.
        Focus on:
        - The current active goal or workflow
        - Any decisions made or data confirmed
        - Remaining blockers or missing information

        FORMAT: A single paragraph of max 150 words.
        
        HISTORY:
        {old_history}
        """

        response = await ai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": "You are a specialized summarization sub-agent."},
                      {"role": "user", "content": summary_prompt}]
        )

        new_summary = response.choices[0].message.content
        
        # 2. Update Persisted Metadata
        metadata["history_summary"] = new_summary
        metadata["last_summary_turn_count"] = len(history)
        
        await calendar_service.sync_client_session({
            "metadata": metadata
        })
        
        logger.info(f"[MEMORY-AGENT] Saved new history summary for {tenant_id}")

    except Exception as e:
        logger.error(f"[MEMORY-AGENT] Summarization failed: {e}", exc_info=True)

def get_memory_recovery(metadata, db_session):
    """
    Retrieves global facts for injection into the system prompt.
    Used by agent_manager.get_rehydration_context.
    """
    global_facts = metadata.get("global_facts", {})
    history_summary = metadata.get("history_summary")
    
    blocks = []
    
    if global_facts:
        blocks.append({
            "header": "### USER KNOWLEDGE (GLOBAL FACTS) ###",
            "data": global_facts,
            "instruction": "These are persistent facts about the user. Do not ask for them again unless the user explicitly corrects them."
        })
        
    if history_summary:
         blocks.append({
            "header": "### RECAP (OLD CONVERSATION SUMMARY) ###",
            "data": {"summary": history_summary},
            "instruction": "This is a summary of the conversation before the current window. Use it to maintain continuity."
        })

    return blocks if blocks else None

async def handle_recall(func_name, args, tenant_id, metadata, db_session, ai_client):
    """
    Tier 2/3: Semantic Recall.
    Attempts to find specific details from past conversations.
    In the first version, this searches the extracted Facts and Summaries.
    In future versions, this will trigger a Vector Search.
    """
    query = args.get("query", "").lower()
    global_facts = metadata.get("global_facts", {})
    history_summary = metadata.get("history_summary", "")
    
    logger.info(f"[MEMORY-AGENT] Recall requested: '{query}' for {tenant_id}")
    
    # 1. Simple Keyword Match in Facts
    found_facts = {}
    for k, v in global_facts.items():
        if query in k.lower() or query in str(v).lower():
            found_facts[k] = v
            
    if found_facts:
        return {
            "status": "success",
            "source": "vault_facts",
            "recalled_info": found_facts,
            "message": f"I found the following related facts in our history: {json.dumps(found_facts)}"
        }
        
    # 2. Check Summary
    if history_summary and query in history_summary.lower():
         return {
            "status": "success",
            "source": "history_summary",
            "recalled_info": history_summary,
            "message": "Found a related segment in our conversation recap. Here is what I remember: " + history_summary
        }

    # 3. Pinecone Semantic Search (The Real Thing)
    pinecone = PineconeClient()
    if pinecone.is_configured:
        try:
             # Embed Query
            resp = await ai_client.embeddings.create(
                model="text-embedding-3-small",
                input=query
            )
            query_vector = resp.data[0].embedding
            
            # Search Namespace
            matches = await pinecone.query_namespace(query_vector, namespace=tenant_id, top_k=2)
            
            if matches:
                # Format segments for injection
                snippets = []
                for m in matches:
                    meta = m.get("metadata", {})
                    snippets.append(meta.get("context", meta.get("fact_value", "Unknown segment")))
                
                return {
                    "status": "success",
                    "source": "pinecone_semantic_recall",
                    "recalled_info": snippets,
                    "message": "I searched our long-term memory vault and found these relevant details: " + "\n- ".join(snippets)
                }
        except Exception as e:
            logger.error(f"[MEMORY-RECALL] Pinecone Search failed: {str(e)}")

    # 4. Default (Placeholder for true Vector Search)
    return {
        "status": "partial_success",
        "message": "I'm searching my long-term memory for that specific detail. Based on my current records, I don't see a direct match. Do you remember when we discussed this?",
        "log_trace": f"Recall Query: {query} - No local match found."
    }
