# app/core/config.py
import os
from pydantic import BaseSettings, AnyHttpUrl
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

@lru_cache() # Cache the settings object for performance
def get_settings() -> Settings:
    return Settings()

settings = get_settings()
