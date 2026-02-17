import os
from pathlib import Path
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
    
    # API Keys (Required)
    OPENAI_API_KEY: str
    
    # Sentry
    SENTRY_DSN: str = "11111111"

    # This config tells Pydantic to look for a .env file
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding='utf-8',
        extra='ignore' # Ignores extra variables in .env
    )

# Create a singleton instance to be used across the app
settings = Settings()