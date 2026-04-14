import os
import pytest

# Disable security and analytics features for the test suite
from src.config import settings
os.environ["NODE_REMOTE_SERVICE_URL"] = settings.NODE_SERVICE_URL
os.environ["JWT_ENABLED"] = "False"
os.environ["SENTRY_DSN"] = ""
os.environ["TLS_VERIFY"] = "False"

@pytest.fixture(autouse=True)
def global_mocks(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from src.config import settings
    
    # 1. Config Mocks
    monkeypatch.setattr(settings, "JWT_ENABLED", False)
    monkeypatch.setattr(settings, "TLS_VERIFY", False)
    
    # 2. Redis Mocks
    mock_redis = MagicMock()
    mock_redis.append_messages = AsyncMock()
    mock_redis.get_history = AsyncMock(return_value=[])
    mock_redis.clear_history = AsyncMock()
    mock_redis.close = AsyncMock()
    mock_redis.redis.ping = AsyncMock(return_value=True)
    
    def mock_init(self, tenant_id, thread_id, user_email=None):
        self.tenant_id = tenant_id
        self.thread_id = thread_id
        self.user_email = user_email
        self.redis = mock_redis.redis
        self.append_messages = mock_redis.append_messages
        self.get_history = mock_redis.get_history
        self.clear_history = mock_redis.clear_history
        self.close = mock_redis.close

    monkeypatch.setattr("src.remote_services.redis_memory.RedisMemoryClient.__init__", mock_init)
    monkeypatch.setattr("src.remote_services.redis_memory.RedisMemoryClient.append_messages", mock_redis.append_messages)
    monkeypatch.setattr("src.remote_services.redis_memory.RedisMemoryClient.get_history", mock_redis.get_history)
    monkeypatch.setattr("src.remote_services.redis_memory.RedisMemoryClient.clear_history", mock_redis.clear_history)
    monkeypatch.setattr("src.remote_services.redis_memory.RedisMemoryClient.close", mock_redis.close)
