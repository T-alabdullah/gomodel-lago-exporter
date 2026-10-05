"""All exporter settings in one place.

Every setting can be overridden with an environment variable named
EXPORTER_<FIELD_NAME> (e.g. EXPORTER_BILL_CACHE_HITS=false) or in a .env file.
The defaults match docs/decisions.md. Changing behaviour should mean changing
config, never code.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class MappingSource(StrEnum):
    """Where a usage row's Lago subscription id can come from."""

    LABEL = "label"          # an API key label like "lago:sub_acme"
    USER_PATH = "user_path"  # the last segment of user_path, e.g. "/customers/sub_acme"


class CacheWriteBilling(StrEnum):
    """What to do with prompt-cache *write* tokens (see decisions.md, D6)."""

    UNCACHED_INPUT = "uncached_input"  # bill them as normal input tokens
    IGNORE = "ignore"                  # don't bill them


CsvList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="EXPORTER_",
        env_file=".env",
        extra="ignore",
    )

    # --- Connections -------------------------------------------------------
    gomodel_db_url: str = "postgresql://exporter_ro:exporter_ro@localhost:5433/gomodel"
    state_db_url: str = "postgresql://postgres:exporter@localhost:5434/exporter"
    lago_api_url: str = "http://localhost:3000"
    lago_api_key: SecretStr = SecretStr("")

    # --- What gets billed (decisions.md D1, D2, D6) ------------------------
    billable_providers: CsvList = ["ollama-qai"]
    bill_cache_hits: bool = True
    cache_write_billing: CacheWriteBilling = CacheWriteBilling.UNCACHED_INPUT

    # --- Customer mapping (decisions.md D2, D3) ----------------------------
    mapping_order: Annotated[list[MappingSource], NoDecode] = [
        MappingSource.LABEL,
        MappingSource.USER_PATH,
    ]
    subscription_label_prefix: str = "lago:"

    # --- Lago metrics (decisions.md D4) ------------------------------------
    metric_input: str = "llm_input_tokens"
    metric_cached_input: str = "llm_cached_input_tokens"
    metric_output: str = "llm_output_tokens"

    # --- Reading GoModel ---------------------------------------------------
    read_batch_size: int = Field(default=500, ge=1)
    overlap_window_seconds: int = Field(default=600, ge=0)
    poll_interval_seconds: float = Field(default=10.0, gt=0)

    # --- Sending to Lago ---------------------------------------------------
    lago_batch_size: int = Field(default=100, ge=1, le=100)  # Lago's max is 100
    http_timeout_seconds: float = Field(default=10.0, gt=0)
    max_retries: int = Field(default=8, ge=0)
    backoff_base_seconds: float = Field(default=0.5, gt=0)
    backoff_max_seconds: float = Field(default=30.0, gt=0)

    # --- Monitoring (decisions.md D5) --------------------------------------
    lag_alert_seconds: int = Field(default=900, ge=1)
    status_port: int = 8000

    @field_validator("billable_providers", "mapping_order", mode="before")
    @classmethod
    def _split_csv(cls, value):
        """Accept 'a,b,c' from env vars as well as real lists."""
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()