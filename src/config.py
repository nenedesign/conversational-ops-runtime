from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql://cor:cor_secret@localhost:5432/cor_dev"
    api_key: str = "dev-api-key-001"
    tenant_id: str = "tenant_dev_001"
    anthropic_api_key: str = ""
    api_version: str = "2026-09-01"


settings = Settings()
