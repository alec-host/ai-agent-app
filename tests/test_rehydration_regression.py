import pytest
import json
from src.agent_manager import get_rehydration_context

@pytest.mark.asyncio
async def test_rehydration_aggregator_calendar():
    """Verify that calendar rehydration still works after CORE SYSTEM STATUS removal."""
    tenant_id = "test-rehydration"
    
    # Mock services
    async def mock_get_session(t):
        return {
            "metadata": {
                "active_workflow": "calendar",
                "event_draft": {
                    "title": "Deposition Room A",
                    "startTime": "2026-03-20T10:00:00Z"
                }
            }
        }
    
    mock_services = {
        'calendar': type('obj', (object,), {
            'get_client_session': mock_get_session
        })
    }
    
    result = await get_rehydration_context(tenant_id, mock_services)
    
    # Assertions
    assert result is not None
    assert "### DATABASE VAULT (CURRENT SESSION STATE) ###" in result["injection"]
    assert "Deposition Room A" in result["injection"]
    # Verify CORE SYSTEM STATUS is NOT there
    assert "CORE SYSTEM STATUS" not in result["injection"]

@pytest.mark.asyncio
async def test_rehydration_aggregator_client():
    """Verify that client rehydration still works after CORE SYSTEM STATUS removal."""
    tenant_id = "test-rehydration-client"
    
    async def mock_get_session(t):
        return {
            "metadata": {
                "active_workflow": "client",
                "client_draft": {
                    "first_name": "Charlie",
                    "client_type": "individual"
                }
            }
        }
    
    mock_services = {
        'calendar': type('obj', (object,), {
            'get_client_session': mock_get_session
        })
    }
    
    result = await get_rehydration_context(tenant_id, mock_services)
    
    assert result is not None
    assert "### RECOVERY MODE: CLIENT INTAKE DETECTED ###" in result["injection"]
    assert "Charlie" in result["injection"]
    assert "CORE SYSTEM STATUS" not in result["injection"]
