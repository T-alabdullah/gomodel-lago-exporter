"""Small helpers shared by tests."""

import uuid
from datetime import datetime, timezone

from exporter.config import Settings
from exporter.contracts import UsageRow

T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def settings(**overrides) -> Settings:
    """Default settings, ignoring any .env file, with optional overrides."""
    return Settings(_env_file=None, **overrides)


def usage_row(**overrides) -> UsageRow:
    """A typical billable usage row (acme, ollama-qai); override any field."""
    fields = dict(
        id=str(uuid.uuid4()),
        request_id="req-1",
        timestamp=T0,
        model="qwen2.5:0.5b",
        provider="ollama",
        provider_name="ollama-qai",
        endpoint="/v1/chat/completions",
        user_path="/",
        labels=("lago:sub_acme",),
        cache_type=None,
        input_tokens=35,
        output_tokens=5,
        total_tokens=40,
        raw_data={},
    )
    fields.update(overrides)
    return UsageRow(**fields)