"""Environment-driven settings. No secrets are defaulted; see ../../.env.example."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"), env_file_encoding="utf-8", extra="ignore",
        str_strip_whitespace=True,  # a stray space inside a quoted .env value (e.g. LLM_MODEL="gpt-5-mini ") must not become part of the value
    )

    app_env: str = "development"
    data_dir: Path = REPO_ROOT / "data"
    frontend_dist: Path = REPO_ROOT / "frontend" / "dist"  # served at / when it exists (npm run build)
    database_url: str = ""
    log_level: str = "INFO"
    app_api_token: str = ""
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    max_upload_mb: int = 25
    max_note_chars: int = 200_000
    fetch_timeout_s: float = 15.0
    fetch_max_mb: int = 5
    fetch_max_redirects: int = 5

    hindsight_base_url: str = "http://127.0.0.1:8890"  # this project's dedicated Hindsight (docker-compose.hindsight.yml)
    hindsight_api_key: str = ""
    hindsight_bank_prefix: str = "pra-"
    hindsight_timeout_s: float = 120.0
    hindsight_required: bool = True
    memory_max_findings_per_source: int = 20

    llm_provider: str = "none"  # none | openai_compatible
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 2

    embedding_provider: str = "none"  # none | openai_compatible
    embedding_base_url: str = "https://api.openai.com/v1"
    embedding_api_key: str = ""
    embedding_model: str = ""

    worker_enabled: bool = True
    worker_poll_s: float = 1.0
    worker_auto_resume_max: int = 2
    retry_backoff_base_s: float = 1.0

    auto_synthesize: bool = True
    auto_update_summary: bool = False

    @field_validator("llm_provider", "embedding_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in {"none", "openai_compatible"}:
            raise ValueError("provider must be 'none' or 'openai_compatible'")
        return v

    @property
    def db_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(Path(self.data_dir) / 'app.db').as_posix()}"

    @property
    def originals_dir(self) -> Path:
        return Path(self.data_dir) / "originals"

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
