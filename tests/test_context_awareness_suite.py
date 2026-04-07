import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from src.agent_manager import get_rehydration_context
from src.agents.memory_agent import extract_and_save_facts, handle_recall, get_memory_recovery
from src.utils import get_starter_chips

@pytest.mark.asyncio
async def test_rehydration_multi_block_injection():
    """
    Verifies that the rehydration aggregator correctly assembles 
    multiple memory blocks (Facts, Summary) into the system prompt.
    """
    mock_services = {"calendar": AsyncMock()}
    mock_db_session = {
        "metadata": {
            "global_facts": {"role": "Senior Partner", "timezone": "Africa/Nairobi"},
            "history_summary": "Discussed setting up a client for John Doe.",
            "active_workflow": "contact"
        }
    }
    mock_services["calendar"].get_client_session.return_value = mock_db_session

    result = await get_rehydration_context("tenant_123", mock_services)
    
    assert result is not None
    assert "### USER KNOWLEDGE (GLOBAL FACTS) ###" in result["injection"]
    assert "### RECAP (OLD CONVERSATION SUMMARY) ###" in result["injection"]
    assert "Senior Partner" in result["injection"]
    assert "John Doe" in result["injection"]

@pytest.mark.asyncio
async def test_fact_extraction_and_mysql_sync():
    """
    Verifies that new facts extracted from history are saved to the MySQL metadata vault.
    """
    mock_ai_client = AsyncMock()
    mock_ai_client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=json.dumps({"facts": {"favorite_topic": "Contract Law"}})))]
    )
    # Mocking the actual embedding creation as it's backgrounded
    mock_ai_client.embeddings.create.return_value = MagicMock(data=[MagicMock(embedding=[0.1]*1536)])

    mock_calendar_service = AsyncMock()
    mock_calendar_service.get_client_session.return_value = {"metadata": {}}
    mock_services = {"calendar": mock_calendar_service}

    history = [
        {"role": "user", "content": "I am a legal expert in contract law."},
        {"role": "assistant", "content": "Got it. I'll remember that."}
    ]

    await extract_and_save_facts("tenant_123", history, mock_services, mock_ai_client)

    # Check if sync_client_session was called with the new fact
    called_args = mock_calendar_service.sync_client_session.call_args[0][0]
    metadata = called_args["metadata"]
    assert metadata["global_facts"]["favorite_topic"] == "Contract Law"

@pytest.mark.asyncio
async def test_proactive_resumption_chips():
    """
    Verifies that the starter chips dynamically suggest resuming unfinished drafts.
    """
    # 1. No Drafts
    chips = get_starter_chips({})
    assert not any("Resume" in c["label"] for c in chips)

    # 2. Contact Draft exists
    chips_with_draft = get_starter_chips({"contact_draft": {"first_name": "Jane"}})
    resume_labels = [c["label"] for c in chips_with_draft if "Resume" in c["label"]]
    assert "🔄 Resume Contact" in resume_labels
    assert len(chips_with_draft) <= 4

@pytest.mark.asyncio
async def test_semantic_recall_pinecone_fallback():
    """
    Verifies that Phase C Semantic Recall successfully queries Pinecone 
    and handles missing configuration gracefully.
    """
    # Mock Config to ensure is_configured returns True for testing the search logic
    with patch("src.agents.memory_agent.PineconeClient") as MockPinecone:
        mock_p_client = MockPinecone.return_value
        mock_p_client.is_configured = True
        mock_p_client.query_namespace = AsyncMock(return_value=[
            {"metadata": {"context": "The user mentioned a $500 retainer yesterday."}}
        ])

        mock_ai_client = AsyncMock()
        mock_ai_client.embeddings.create.return_value = MagicMock(data=[MagicMock(embedding=[0.1]*1536)])

        args = {"query": "retainer fee"}
        metadata = {}
        
        result = await handle_recall("recall_past_conversation", args, "tenant_123", metadata, {}, mock_ai_client)
        
        assert result["status"] == "success"
        assert result["source"] == "pinecone_semantic_recall"
        assert "$500" in result["message"]

@pytest.mark.asyncio
async def test_memory_layer_resilience():
    """
    Ensures that if Pinecone is not configured, the agent still falls back to keyword matching 
    on the MySQL cache (Tier 2).
    """
    with patch("src.agents.memory_agent.PineconeClient") as MockPinecone:
        mock_p_client = MockPinecone.return_value
        mock_p_client.is_configured = False # Pinecone disabled

        args = {"query": "Nairobi"}
        metadata = {
            "global_facts": {"office_location": "Nairobi, Kenya"}
        }
        
        result = await handle_recall("recall_past_conversation", args, "tenant_123", metadata, {}, AsyncMock())
        
        # Should succeed using local facts despite Pinecone being offline
        assert result["status"] == "success"
        assert result["source"] == "vault_facts"
        assert "Nairobi" in result["message"]

@pytest.mark.asyncio
async def test_automated_state_purging():
    """
    Verifies that when a tool returns _exit_loop=True, the dispatcher 
    automatically triggers a state purge for the corresponding draft.
    """
    from src.agent_manager import execute_tool_call
    
    # 1. Mock Specialist Result (Terminal Success)
    mock_result = {
        "status": "success", 
        "message": "Contact Created!", 
        "_exit_loop": True
    }
    
    mock_tool = MagicMock()
    mock_tool.function.name = "create_contact"
    mock_tool.function.arguments = json.dumps({"first_name": "Jane"})

    mock_calendar_service = AsyncMock()
    # Mocking successful session fetch
    mock_calendar_service.get_client_session.return_value = {"metadata": {"active_workflow": "contact", "contact_draft": {"first_name": "Jane"}}}
    
    mock_services = {"calendar": mock_calendar_service}

    # Patch handle_core_ops to return our terminal success
    with patch("src.agent_manager.handle_core_ops", new_callable=AsyncMock) as mock_core:
        mock_core.return_value = mock_result
        
        await execute_tool_call(mock_tool, mock_services, "user", "tenant_123", [], ai_client=AsyncMock())

        # Verify SYNC was called to PURGE
        # It should be called once in the dispatcher 'finally' block
        assert mock_calendar_service.sync_client_session.called
        purge_payload = mock_calendar_service.sync_client_session.call_args[0][0]
        
        # Verify purge values
        assert purge_payload["metadata"]["active_workflow"] is None
        assert purge_payload["metadata"]["contact_draft"] == {}
        assert purge_payload["metadata"]["session_lifecycle"] == "completed"
