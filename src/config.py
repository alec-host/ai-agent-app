import os
from pathlib import Path
from typing import Optional, List
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "MatterMiner Legal Agentic AI"
    APP_DESCRIPTION: str = "Orchestration layer for Legal Calendar Operations with strict Tenant isolation."
    DEBUG: bool = False

    # External Service URLs
    # Defaults to localhost if not found in .env
    NODE_SERVICE_URL: str = "http://localhost:3005"
    NODE_REMOTE_SERVICE_URL: str = "https://dev.matterminer.com"
    
    # API Keys (Required)
    OPENAI_API_KEY: str
    PINECONE_API_KEY: str = "" # Optional defaults to prevent breaking local dev
    PINECONE_HOST: str = ""    # The full URL for the index (e.g., https://...pinecone.io)
    PINECONE_INDEX_NAME: str = "matterminer-memory"
    
    # Redis (Memory/Context Awareness)
    REDIS_PASS: str = ""
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6379

    # CORS — Allowed Origins (SEC-02)
    CORS_ALLOWED_ORIGINS: list = [
        "https://app.matterminer.com",
        "https://dev.matterminer.com",
        "http://localhost:3000",
    ]

    # TLS Verification for outbound HTTP clients (SEC-03)
    # Set to True (default, secure), False (INSECURE — dev only), or a CA bundle path string
    TLS_VERIFY: bool = True

    # Sentry
    SENTRY_DSN: str = ""

    # JWT Verification (SEC-05)
    # Confirm with Node.js backend: HS256 or RS256?
    JWT_SECRET: str = "your-placeholder-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_AUDIENCE: Optional[str] = None
    JWT_ENABLED: bool = True # Set to False only for local testing without Core backend

    # Supported Timezones for Event Creation
    SUPPORTED_TIMEZONES: list = [
        {"label": "Nairobi (EAT)", "value": "Africa/Nairobi"},
        {"label": "US Eastern (EST/EDT)", "value": "America/New_York"},
        {"label": "US Pacific (PST/PDT)", "value": "America/Los_Angeles"},
        {"label": "Europe London (GMT/BST)", "value": "Europe/London"},
        {"label": "UTC", "value": "UTC"}
    ]

    # This config tells Pydantic to look for a .env file
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding='utf-8',
        extra='ignore' # Ignores extra variables in .env
    )

# Create a singleton instance to be used across the app
settings = Settings()