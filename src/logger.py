import logging
import time
from logging.handlers import RotatingFileHandler
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from .config import settings

def setup_logging():
    """Configures Sentry and Local File Logging."""
    
    # 1. Initialize Sentry
    if settings.SENTRY_DSN and settings.SENTRY_DSN.startswith("http"):
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            integrations=[FastApiIntegration()],
            traces_sample_rate=1.0,
            profiles_sample_rate=1.0,
            send_default_pii=True # Helps identify tenant issues
        )

    # 2. Define Format
    log_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(log_format)

    # 3. Local File Handler (app.log)
    file_handler = RotatingFileHandler(
        "app.log", 
        maxBytes=5*1024*1024, 
        backupCount=3
    )
    file_handler.setFormatter(formatter)

    # 4. Stream Handler (Console/PowerShell)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)

    # 5. Root Logger Configuration
    logger = logging.getLogger("legal-agentic-ai")
    logger.setLevel(logging.INFO)
    
    # Avoid duplicate logs if setup is called twice
    if not logger.handlers:
        logger.addHandler(file_handler)
        logger.addHandler(stream_handler)

    return logger

# Create the logger instance to be imported elsewhere
logger = setup_logging()