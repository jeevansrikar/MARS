from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore", hide_input_in_errors=True)
    environment: Literal["development", "testing", "staging", "production"] = "development"
    database_url: str = "postgresql+asyncpg://mars@postgres:5432/mars"
    redis_url: str = "redis://redis:6379/0"
    redis_max_connections: int = 50
    allowed_origins: list[str] = ["http://localhost:3000"]
    anthropic_api_key: SecretStr = SecretStr("")
    search_api_key: SecretStr = SecretStr("")
    research_model: str = "claude-sonnet-4-6"
    fast_model: str = "claude-haiku-4-5"
    synthesis_model: str = "claude-sonnet-4-6"
    verification_model: str = "claude-sonnet-4-6"
    provider_timeout: float = Field(default=90, gt=0)
    provider_retries: int = Field(default=2, ge=0, le=5)
    max_output_tokens: int = Field(default=2048, ge=256, le=8192)
    max_input_bytes: int = Field(default=96000, ge=1000, le=250000)
    model_prices: dict[str, tuple[float, float]] = Field(default_factory=dict)
    semantic_enabled: bool = True
    embedding_cache_dir: str = ".model-cache"
    embedding_revision: str = "d13954661f83248295ba75c1ed411eef3b7b936e"
    max_agent_calls: int = Field(default=32, ge=12, le=100)
    max_search_calls: int = Field(default=4, ge=1, le=10)
    max_sources: int = Field(default=8, ge=1, le=20)
    max_source_bytes: int = Field(1_000_000, ge=1024, le=5_000_000)
    max_document_chars: int = Field(default=24000, ge=1000, le=50000)
    max_job_seconds: int = Field(default=900, ge=10, le=3600)
    max_attempts: int = Field(default=3, ge=1, le=5)
    lease_seconds: int = Field(default=45, ge=6)
    worker_concurrency: int = Field(default=2, ge=1, le=16)
    max_active_jobs: int = Field(default=2, ge=1, le=20)
    daily_job_quota: int = Field(default=20, ge=1)
    max_debate_rounds: int = Field(default=2, ge=0, le=4)
    api_rate_per_minute: int = Field(default=120, ge=1)
    session_days: int = Field(default=7, ge=1, le=30)
    memory_ttl_days: int = Field(default=90, ge=1, le=365)
    metrics_token: SecretStr = SecretStr("")
    otel_exporter_otlp_endpoint: str = ""

    @model_validator(mode="after")
    def validate_production(self):
        if any(rate < 0 for prices in self.model_prices.values() for rate in prices):
            raise ValueError("Model prices must be nonnegative")
        if self.environment in {"production", "staging"}:
            if not self.database_url.startswith("postgresql+asyncpg://"):
                raise ValueError("Production requires PostgreSQL")
            if not self.allowed_origins or any(not x.startswith("https://") for x in self.allowed_origins):
                raise ValueError("Explicit HTTPS origins are required")
            if not self.anthropic_api_key.get_secret_value() or not self.search_api_key.get_secret_value():
                raise ValueError("Provider credentials are required")
            if len(self.metrics_token.get_secret_value()) < 32:
                raise ValueError("An operator metrics token of at least 32 characters is required")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


# Compatibility for the original working-memory library.
settings = get_settings()
