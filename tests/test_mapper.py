"""Tests for the mapper: who pays for each usage row."""

from exporter.config import MappingSource
from exporter.contracts import MappingOutcome
from exporter.mapper import map_row, subscription_from_user_path
from tests.factories import settings, usage_row

DEFAULTS = settings()


# --- Billable or not -----------------------------------------------------

def test_non_billable_provider_is_not_billable():
    result = map_row(usage_row(provider_name="ollama-ext"), DEFAULTS)
    assert result.outcome is MappingOutcome.NOT_BILLABLE
    assert "ollama-ext" in result.reason


def test_missing_provider_name_is_not_billable():
    assert map_row(usage_row(provider_name=None), DEFAULTS).outcome is MappingOutcome.NOT_BILLABLE


def test_cache_hits_are_billed_by_default():
    result = map_row(usage_row(cache_type="exact"), DEFAULTS)
    assert result.outcome is MappingOutcome.MAPPED


def test_cache_hits_not_billed_when_switched_off():
    result = map_row(usage_row(cache_type="exact"), settings(bill_cache_hits=False))
    assert result.outcome is MappingOutcome.NOT_BILLABLE


# --- Who pays ------------------------------------------------------------

def test_label_maps_to_subscription():
    result = map_row(usage_row(labels=("lago:sub_acme",)), DEFAULTS)
    assert (result.outcome, result.external_subscription_id) == (MappingOutcome.MAPPED, "sub_acme")


def test_user_path_is_the_fallback():
    result = map_row(usage_row(labels=(), user_path="/customers/sub_beta"), DEFAULTS)
    assert (result.outcome, result.external_subscription_id) == (MappingOutcome.MAPPED, "sub_beta")


def test_label_wins_over_user_path():
    # Protects labelled keys from a spoofed X-GoModel-User-Path header (decisions.md R1).
    row = usage_row(labels=("lago:sub_acme",), user_path="/customers/sub_beta")
    assert map_row(row, DEFAULTS).external_subscription_id == "sub_acme"


def test_mapping_order_is_configurable():
    row = usage_row(labels=("lago:sub_acme",), user_path="/customers/sub_beta")
    s = settings(mapping_order=[MappingSource.USER_PATH, MappingSource.LABEL])
    assert map_row(row, s).external_subscription_id == "sub_beta"


def test_other_labels_are_ignored():
    row = usage_row(labels=("team:research", "lago:sub_acme"))
    assert map_row(row, DEFAULTS).external_subscription_id == "sub_acme"


def test_same_label_twice_is_fine():
    row = usage_row(labels=("lago:sub_acme", "lago:sub_acme"))
    assert map_row(row, DEFAULTS).external_subscription_id == "sub_acme"


# --- Nobody to bill ------------------------------------------------------

def test_no_label_and_root_user_path_is_unmapped():
    # This is the "ghost" key from Step 2.
    result = map_row(usage_row(labels=(), user_path="/"), DEFAULTS)
    assert result.outcome is MappingOutcome.UNMAPPED
    assert result.external_subscription_id is None
    assert result.reason == "no lago: label; no user_path"


def test_conflicting_labels_are_unmapped_never_guessed():
    row = usage_row(labels=("lago:sub_acme", "lago:sub_beta"), user_path="/customers/sub_beta")
    result = map_row(row, DEFAULTS)
    assert result.outcome is MappingOutcome.UNMAPPED
    assert "conflicting" in result.reason


def test_empty_label_value_is_ignored():
    result = map_row(usage_row(labels=("lago:",), user_path="/"), DEFAULTS)
    assert result.outcome is MappingOutcome.UNMAPPED


# --- user_path rule (decisions.md D3) ------------------------------------

def test_user_path_last_segment():
    assert subscription_from_user_path("/customers/sub_beta") == "sub_beta"
    assert subscription_from_user_path("/customers/sub_beta/") == "sub_beta"
    assert subscription_from_user_path("/") is None
    assert subscription_from_user_path("") is None
    assert subscription_from_user_path(None) is None