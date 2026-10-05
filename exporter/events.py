"""Turn a mapped usage row into Lago events: up to 3, one per token type.

Input tokens are split into uncached / cached / cache-write exactly the way
GoModel itself does it (EntryInputSegments in internal/usage/request_summary.go,
GoModel 0.1.99), so cached tokens are never billed twice.
"""

from exporter.config import CacheWriteBilling, Settings
from exporter.contracts import LagoEvent, MappingOutcome, MappingResult, TokenKind, UsageRow

# Property names must match each billable metric's field_name in Lago
# (see scripts/lago_setup.py).
FIELD_NAMES = {
    TokenKind.INPUT: "input_tokens",
    TokenKind.CACHED: "cached_input_tokens",
    TokenKind.OUTPUT: "output_tokens",
}


def _raw_int(raw: dict, key: str) -> int:
    """A whole number from raw_data, or 0. Mirrors GoModel's extractInt."""
    value = raw.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def input_segments(row: UsageRow) -> tuple[int, int, int]:
    """Split input tokens into (uncached, cached, cache_write), like GoModel."""
    raw = row.raw_data
    cache_read_top_level = _raw_int(raw, "cache_read_input_tokens")   # Anthropic style
    cache_read_normalized = _raw_int(raw, "prompt_cached_tokens")     # OpenAI style
    cache_read_generic = _raw_int(raw, "cached_tokens")
    cache_write = max(_raw_int(raw, "cache_creation_input_tokens"),
                      _raw_int(raw, "cache_write_input_tokens"))
    cached = max(cache_read_top_level, cache_read_normalized, cache_read_generic)
    base = max(row.input_tokens, 0)

    # Some providers (Anthropic) report input_tokens WITHOUT the cached part:
    # cache reads and writes come on top. Then input_tokens is all uncached.
    split_accounting = (
        cache_read_top_level > 0
        or cache_write > 0
        or row.provider.strip().lower() == "anthropic"
    )
    if split_accounting:
        return base, cached, cache_write

    # Otherwise (OpenAI style) input_tokens INCLUDES the cached part.
    cached = min(cached, base)
    return base - cached, cached, cache_write


def build_events(mapping: MappingResult, settings: Settings) -> list[LagoEvent]:
    """Up to three events for one mapped row; token counts of 0 are skipped."""
    if mapping.outcome is not MappingOutcome.MAPPED or not mapping.external_subscription_id:
        raise ValueError(f"row {mapping.row.id} is not mapped ({mapping.outcome})")
    row = mapping.row

    uncached, cached, cache_write = input_segments(row)
    if settings.cache_write_billing is CacheWriteBilling.UNCACHED_INPUT:
        uncached += cache_write   # decisions.md D6

    tokens = {
        TokenKind.INPUT: uncached,
        TokenKind.CACHED: cached,
        TokenKind.OUTPUT: max(row.output_tokens, 0),
    }
    codes = {
        TokenKind.INPUT: settings.metric_input,
        TokenKind.CACHED: settings.metric_cached_input,
        TokenKind.OUTPUT: settings.metric_output,
    }

    events = []
    for kind, count in tokens.items():
        if count <= 0:
            continue
        events.append(LagoEvent(
            usage_row_id=row.id,
            kind=kind,
            code=codes[kind],
            external_subscription_id=mapping.external_subscription_id,
            timestamp=row.timestamp,            # the row's time, never "now"
            tokens=count,
            properties={
                "model": row.model,
                FIELD_NAMES[kind]: count,
                "provider_name": row.provider_name or "",
                "cache_type": row.cache_type or "",
            },
        ))
    return events