import json
import httpx
from src.logger import logger
from src.config import settings

class PineconeClient:
    """
    Lean REST-based client for Pinecone Vector Index operations.
    Optimized for high-concurrency async operations without heavy SDK overhead.
    """
    def __init__(self):
        self.api_key = settings.PINECONE_API_KEY
        self.host = settings.PINECONE_HOST # e.g. https://index-name-id.svc.env.pinecone.io
        self.headers = {
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
        
        if not self.api_key or not self.host:
            logger.warning("[PINECONE] Keys or Host missing in settings. Real-time recall will be disabled.")

    @property
    def is_configured(self):
        return bool(self.api_key and self.host)

    async def upsert_vectors(self, vectors: list, namespace: str = "default"):
        """
        Upserts embeddings into the index for a specific tenant (namespace).
        Payload per vector: {"id": "...", "values": [...], "metadata": {...}}
        """
        if not self.is_configured:
            return None

        url = f"{self.host}/vectors/upsert"
        payload = {
            "vectors": vectors,
            "namespace": namespace
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                logger.debug(f"[PINECONE] Successfully upserted {len(vectors)} vectors into namespace: {namespace}")
                return response.json()
            except Exception as e:
                logger.error(f"[PINECONE-UPSERT] Failed: {e}")
                return None

    async def query_namespace(self, vector: list, namespace: str, top_k: int = 3):
        """
        Performs semantic search within a tenant's specific namespace.
        """
        if not self.is_configured:
            return []

        url = f"{self.host}/query"
        payload = {
            "vector": vector,
            "topK": top_k,
            "includeMetadata": True,
            "namespace": namespace
        }
        
        async with httpx.AsyncClient(timeout=10) as client:
            try:
                response = await client.post(url, json=payload, headers=self.headers)
                response.raise_for_status()
                data = response.json()
                return data.get("matches", [])
            except Exception as e:
                logger.error(f"[PINECONE-QUERY] Failed: {e}")
                return []
