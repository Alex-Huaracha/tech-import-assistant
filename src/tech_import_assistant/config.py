"""Settings: paths, download parameters, chunking parameters, embedding model.

Every field can be overridden with an environment variable prefixed TIA_
(e.g. TIA_HTTP_TIMEOUT_SECONDS=120) or in a .env file at the project root.
Secrets such as the LLM API key will live in .env, which is not committed.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root: src/tech_import_assistant/config.py -> three levels up
ROOT_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TIA_",
        env_file=ROOT_DIR / ".env",
        extra="ignore",
    )

    # --- Paths -------------------------------------------------------------
    sources_file: Path = ROOT_DIR / "sources.toml"
    raw_dir: Path = ROOT_DIR / "data" / "raw"
    knowledge_base_dir: Path = ROOT_DIR / "knowledge_base"

    # --- Download ----------------------------------------------------------
    http_user_agent: str = "tech-import-assistant/0.1"
    http_timeout_seconds: float = 60
    seconds_between_requests: float = 1.0  # be polite to the government servers


settings = Settings()
