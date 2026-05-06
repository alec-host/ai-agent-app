import httpx
import logging
from typing import Dict, Any, Optional
from src.config import settings

logger = logging.getLogger("legal-agentic-ai")

def flatten_dict(d: Dict[str, Any], parent_key: str = '', sep: str = '_') -> Dict[str, Any]:
    """
    Recursively flattens a nested dictionary.
    Pinecone requires flat metadata.
    """
    items = []
    for k, v in d.items():
        new_key = f"{parent_key}{sep}{k}" if parent_key else k
        if isinstance(v, dict):
            items.extend(flatten_dict(v, new_key, sep=sep).items())
        else:
            items.append((new_key, v))
    return dict(items)

class RagClient:
    """
    Client for interacting with the external Node.js RAG API (/app/core/rag).
    Delegates all vectorization and Pinecone operations to the Node.js service.
    """
    def __init__(self, tenant_id: str):
        self.tenant_id = tenant_id
        
        self.base_url = settings.NODE_REMOTE_SERVICE_URL.rstrip("/")
        self.root_prefix = "/api" if "/api" in self.base_url.lower() else "/app"
        self.base_url = self.base_url.replace("/api", "").replace("/app", "")
        
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            verify=settings.TLS_VERIFY
        )

    def _get_headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "X-Tenant-ID": self.tenant_id,
            "Accept": "application/json",
            "Authorization": f"Bearer {settings.CORE_API_KEY}"
        }

    async def _post_rag(self, action: str, data: str, metadata: Dict[str, Any]) -> Dict[str, Any]:
        """
        Base method to send requests to the /core/rag endpoint.
        """
        # Ensure metadata is flat
        flat_metadata = flatten_dict(metadata)
        
        # Add tenantId explicitly into the payload as required by the plan
        payload = {
            "tenantId": self.tenant_id,
            "action": action,
            "data": data,
            "metadata": flat_metadata
        }
        
        url = f"{self.base_url}{self.root_prefix}/core/rag"
        logger.info(f"[RAG-CLIENT] Submitting {action} request to {url}")
        
        try:
            response = await self.client.post(url, json=payload, headers=self._get_headers())
            
            if response.status_code in [200, 201]:
                return response.json()
            else:
                logger.error(f"[RAG-CLIENT] Error {response.status_code}: {response.text}")
                return {"status": "error", "code": response.status_code, "message": response.text}
        except Exception as e:
            logger.error(f"[RAG-CLIENT] Exception during RAG API call: {e}")
            return {"status": "error", "message": str(e)}

    async def upsert_coi_record(self, data_str: str, matter_id: str, status: str = "active") -> Dict[str, Any]:
        """
        Upserts a Conflict of Interest record into the Vector DB.
        """
        metadata = {
            "type": "coi_record",
            "matter_id": matter_id,
            "status": status
        }
        return await self._post_rag("upsert", data_str, metadata)

    async def check_coi(self, proposed_name: str) -> Dict[str, Any]:
        """
        Queries the Vector DB using Hybrid Search for Conflict of Interest.
        """
        metadata_filter = {
            "type": "coi_record",
            "status": "active"
        }
        return await self._post_rag("hybrid_search", proposed_name, metadata_filter)

    async def search_past_matters(self, query: str) -> Dict[str, Any]:
        """
        Searches historical matters with Reranking via Node.js API.
        """
        metadata_filter = {"type": "matter_record"}
        return await self._post_rag("search_reranked", query, metadata_filter)

    async def lookup_firm_protocol(self, query: str) -> Dict[str, Any]:
        """
        Looks up active firm protocols using strict temporal metadata filters.
        """
        metadata_filter = {
            "type": "firm_protocol",
            "is_current": True
        }
        return await self._post_rag("search", query, metadata_filter)

    async def close(self):
        await self.client.aclose()
