import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # App Settings
    APP_NAME: str = "Legal Agentic AI"
    DEBUG: bool = False

    # External Service URLs
    # Defaults to localhost if not found in .env
    NODE_SERVICE_URL: str = "http://localhost:3000"
    
    # API Keys (Required)
    OPENAI_API_KEY: str

    # This config tells Pydantic to look for a .env file
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding='utf-8',
        extra='ignore' # Ignores extra variables in .env
    )

# Create a singleton instance to be used across the app
settings = Settings()