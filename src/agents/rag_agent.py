# src/agents/rag_agent.py
from src.logger import logger

async def handle_rag_lookup(func_name, args, services, tenant_id):
    """
    Handles retrieval of firm protocols and search through the legal knowledge base.
    """
    logger.info(f"[{tenant_id}] Handling RAG: {func_name}")
    
    user_query = args.get("query")
    if not user_query:
        return {"status": "error", "message": "No query provided for the RAG lookup."}

    try:
        if func_name == "lookup_firm_protocol":
            # Direct call to the calendar service which manages the RAG connection
            result_context = await services['calendar'].get_workflow_protocol(
                query=user_query, 
                tenant_id=tenant_id
            )
            return {"status": "success", "data": result_context}

        if func_name == "search_knowledge_base":
            # For now, we use the same endpoint as protocol lookup
            # but this can be extended to use a different RAG index if needed
            result_data = await services['calendar'].get_workflow_protocol(
                query=user_query, 
                tenant_id=tenant_id
            )
            return {"status": "success", "data": result_data}

    except Exception as e:
        logger.error(f"[RAG-FAILURE] Error in {func_name}: {e}")
        return {"status": "error", "message": "Failed to retrieve information from the knowledge base."}

    return {"status": "error", "message": f"Tool {func_name} not implemented in RAG agent."}
