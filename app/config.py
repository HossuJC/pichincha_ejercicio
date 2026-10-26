from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_", env_file=ROOT_DIR / ".env", env_file_encoding="utf-8", extra="ignore")

    # Fixed business date so the 60-day window and the 30-day history are reproducible.
    today: date = date(2026, 10, 26)

    # LLM. The key is read from the environment or from .env (never logged: SecretStr).
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("ANTHROPIC_API_KEY", "APP_ANTHROPIC_API_KEY")
    )
    llm_provider: Literal["anthropic", "fake", "custom"] = "anthropic"
    # With llm_provider=custom: "package.module:factory", called with Settings, returning an LLMClient
    # (e.g. a local mock kept out of the repo).
    llm_factory: str | None = None
    model: str = "claude-opus-5-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    max_tokens: int = 16000
    max_iterations: int = 12
    enable_fallbacks: bool = True
    llm_timeout_s: float = 120.0

    # Legacy dispute system (mock)
    dispute_timeout_s: float = 5.0
    dispute_slow_probability: float = 0.3
    dispute_slow_min_s: float = 6.0
    dispute_slow_max_s: float = 8.0
    dispute_fast_max_s: float = 1.0
    dispute_max_attempts: int = 2

    # Merchant provider (mock)
    merchant_timeout_s: float = 2.0
    merchant_cache_ttl_s: float = 3600.0
    merchant_failure_probability: float = 0.0

    # Storage
    session_backend: Literal["memory", "sqlite"] = "memory"
    sqlite_path: Path = ROOT_DIR / "sessions.db"
    reglamento_dir: Path = ROOT_DIR / "docs" / "reglamento"

    @field_validator("anthropic_api_key", mode="before")
    @classmethod
    def _blank_key_is_unset(cls, value):
        # `ANTHROPIC_API_KEY=` copied from .env.example means "not configured".
        return value.strip() or None if isinstance(value, str) else value


@lru_cache
def get_settings() -> Settings:
    return Settings()
