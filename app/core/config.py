# app/core/config.py
import os
from pydantic_settings import BaseSettings
from pydantic import AnyHttpUrl # AnyHttpUrl stays in pydantic core
from functools import lru_cache
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

class Settings(BaseSettings):
    PROJECT_NAME: str = "Swarm API Aggregator"
    API_V1_STR: str = "/api/v1"
    SWARM_BEE_API_URL: AnyHttpUrl # validates that it's a URL

    # Settings for Simple Auth (to be used later)
    # API_KEY: str | None = None
    # API_KEY_NAME: str = "X-API-Key"

    class Config:
        env_file = ".env"
        case_sensitive = True
        extra = "ignore"  # Ignore extra fields from .env

@lru_cache() # Cache the settings object for performance
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
