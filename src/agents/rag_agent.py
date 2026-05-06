# src/agents/rag_agent.py
from src.logger import logger
from src.rag_integrations.rag_client import RagClient

async def handle_rag_lookup(func_name, args, services, tenant_id):
    """
    Handles retrieval of firm protocols and search through the legal knowledge base
    using the Node.js /app/core/rag endpoint.
    """
    logger.info(f"[{tenant_id}] Handling RAG: {func_name}")
    
    user_query = args.get("query")
    if not user_query:
        return {"status": "error", "message": "No query provided for the RAG lookup."}

    client = RagClient(tenant_id)

    try:
        if func_name == "lookup_firm_protocol":
            result = await client.lookup_firm_protocol(user_query)
            return {"status": "success", "data": result}

        if func_name == "search_past_matters":
            result = await client.search_past_matters(user_query)
            return {"status": "success", "data": result}

    except Exception as e:
        logger.error(f"[RAG-FAILURE] Error in {func_name}: {e}")
        return {"status": "error", "message": "Failed to retrieve information from the knowledge base."}
    finally:
        await client.close()

    return {"status": "error", "message": f"Tool {func_name} not implemented in RAG agent."}
