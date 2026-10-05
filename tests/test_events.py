"""Tests for the event builder: what each mapped row turns into."""

from exporter.config import CacheWriteBilling
from exporter.contracts import MappingOutcome, MappingResult, TokenKind
from exporter.events import build_events, input_segments
from tests.factories import T0, settings, usage_row

DEFAULTS = settings()


def events_for(row, s=DEFAULTS):
    return build_events(MappingResult(row, MappingOutcome.MAPPED, "sub_acme"), s)


def tokens_by_kind(events):
    return {e.kind: e.tokens for e in events}


# --- The basic shape -----------------------------------------------------

def test_plain_row_gives_input_and_output_events():
    events = events_for(usage_row(input_tokens=35, output_tokens=5))
    assert tokens_by_kind(events) == {TokenKind.INPUT: 35, TokenKind.OUTPUT: 5}


def test_event_fields_match_the_task_format():
    row = usage_row(output_tokens=412, cache_type=None)
    out = next(e for e in events_for(row) if e.kind is TokenKind.OUTPUT)
    assert out.transaction_id == f"{row.id}:out"
    assert out.external_subscription_id == "sub_acme"
    assert out.code == "llm_output_tokens"
    assert out.timestamp == T0                 # the row's time, not "now"
    assert out.properties == {
        "model": "qwen2.5:0.5b",
        "output_tokens": 412,
        "provider_name": "ollama-qai",
        "cache_type": "",
    }


def test_zero_counts_are_skipped():
    assert events_for(usage_row(input_tokens=0, output_tokens=0)) == []
    assert tokens_by_kind(events_for(usage_row(output_tokens=0))) == {TokenKind.INPUT: 35}


def test_metric_codes_come_from_config():
    s = settings(metric_input="in_v2", metric_output="out_v2")
    assert {e.code for e in events_for(usage_row(), s)} == {"in_v2", "out_v2"}


def test_unmapped_row_cannot_become_events():
    import pytest
    with pytest.raises(ValueError):
        build_events(MappingResult(usage_row(), MappingOutcome.UNMAPPED), DEFAULTS)


# --- Cached tokens are never billed twice --------------------------------

def test_openai_style_cached_tokens_are_split_out_of_input():
    # input_tokens (100) INCLUDES the 30 cached ones.
    row = usage_row(input_tokens=100, raw_data={"prompt_cached_tokens": 30})
    assert tokens_by_kind(events_for(row)) == {
        TokenKind.INPUT: 70, TokenKind.CACHED: 30, TokenKind.OUTPUT: 5,
    }


def test_generic_cached_tokens_key_works_too():
    row = usage_row(input_tokens=100, raw_data={"cached_tokens": 40})
    assert input_segments(row) == (60, 40, 0)


def test_cached_can_never_exceed_input():
    row = usage_row(input_tokens=10, raw_data={"cached_tokens": 50})
    assert input_segments(row) == (0, 10, 0)


def test_anthropic_style_cached_tokens_come_on_top():
    # Anthropic's input_tokens EXCLUDES cache reads, so nothing is subtracted.
    row = usage_row(provider="anthropic", input_tokens=100,
                    raw_data={"cache_read_input_tokens": 40})
    assert input_segments(row) == (100, 40, 0)


def test_cache_write_tokens_billed_as_input_by_default():
    row = usage_row(input_tokens=100, raw_data={"cache_creation_input_tokens": 20})
    assert tokens_by_kind(events_for(row))[TokenKind.INPUT] == 120


def test_cache_write_tokens_can_be_ignored():
    row = usage_row(input_tokens=100, raw_data={"cache_creation_input_tokens": 20})
    s = settings(cache_write_billing=CacheWriteBilling.IGNORE)
    assert tokens_by_kind(events_for(row, s))[TokenKind.INPUT] == 100


def test_weird_raw_data_values_are_ignored():
    row = usage_row(input_tokens=100, raw_data={"cached_tokens": "lots", "prompt_cached_tokens": True})
    assert input_segments(row) == (100, 0, 0)