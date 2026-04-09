import json
import redis.asyncio as redis
from typing import List, Dict, Any
from src.config import settings
from src.logger import logger

class RedisMemoryClient:
    """
    Handles server-side short-term conversational history via Redis.
    Provides automatic context awareness for stateless integrations like Postman
    or third-party plugins that don't pass the 'history' array.
    """
    def __init__(self, tenant_id: str, thread_id: str = "default"):
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        # Namespace keys to prevent collision across tenants/threads
        self.key = f"matterminer:chat_history:{self.tenant_id}:{self.thread_id}"
        
        # Configure Redis with strict timeouts so missing Redis doesn't hang the app
        self.redis = redis.from_url(
            settings.REDIS_URL, 
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2
        )
        self.ttl = 86400  # 24 hours conversation TTL

    async def get_history(self, limit: int = 12) -> List[Dict[str, Any]]:
        """
        Retrieves the last N messages from Redis.
        Returns an empty list [] if Redis is offline or the history is empty.
        """
        try:
            items = await self.redis.lrange(self.key, -limit, -1)
            if not items:
                return []
            return [json.loads(item) for item in items]
        except Exception as e:
            logger.warning(f"[REDIS-MEMORY] Failed to fetch history for {self.key}. Is Redis running? ({e})")
            return []

    async def append_messages(self, messages: List[Dict[str, Any]]):
        """
        Appends new messages to the Redis list and manages memory limits.
        """
        if not messages:
            return
            
        try:
            # Serialize each message to JSON
            serialized = [json.dumps(m) for m in messages]
            
            # Push all serialized messages to the tail of the list
            await self.redis.rpush(self.key, *serialized)
            
            # Trim the list to keep only the most recent 40 messages to prevent unbounded growth
            await self.redis.ltrim(self.key, -40, -1)
            
            # Refresh the TTL for the whole conversation thread
            await self.redis.expire(self.key, self.ttl)
        except Exception as e:
            logger.warning(f"[REDIS-MEMORY] Failed to append to {self.key}. ({e})")

    async def clear_history(self):
        """
        Wipes the conversational history for this thread.
        """
        try:
            await self.redis.delete(self.key)
            logger.info(f"[REDIS-MEMORY] Cleared conversation history for {self.key}.")
        except Exception as e:
            logger.warning(f"[REDIS-MEMORY] Failed to clear history for {self.key}. ({e})")

    async def close(self):
        """
        Creates a clean closure of the Redis connection pool.
        """
        try:
            # Only use aclose if available in the installed redis version
            if hasattr(self.redis, 'aclose'):
                await self.redis.aclose()
            else:
                await self.redis.close()
        except:
            pass
