import os
import pytest

# Disable security and analytics features for the test suite
from src.config import settings
os.environ["NODE_REMOTE_SERVICE_URL"] = settings.NODE_SERVICE_URL
os.environ["JWT_ENABLED"] = "False"
os.environ["SENTRY_DSN"] = ""
os.environ["TLS_VERIFY"] = "False"

@pytest.fixture(autouse=True)
def disable_jwt(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "JWT_ENABLED", False)
    monkeypatch.setattr(settings, "TLS_VERIFY", False)
