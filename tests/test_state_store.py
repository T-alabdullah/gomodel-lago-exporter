"""Tests for the exporter's state database.

These need the exporter's Postgres running (`docker compose up -d`).
They use a separate database, `exporter_test`, so real state is never touched.
If Postgres isn't reachable, the tests are skipped instead of failing.
"""

import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from exporter.contracts import RowStatus, TokenKind, UsageRow
from exporter.state.store import Cursor, StateStore

ADMIN_DSN = os.environ.get(
    "EXPORTER_TEST_ADMIN_DB_URL", "postgresql://postgres:exporter@localhost:5434/postgres"
)
TEST_DB = "exporter_test"
T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def _test_db_url() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture(scope="session")
def _test_database():
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3) as conn:
            exists = conn.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)
            ).fetchone()
            if not exists:
                conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    except psycopg.OperationalError as e:
        pytest.skip(f"exporter Postgres not reachable ({e.__class__.__name__}); run docker compose up -d")


@pytest.fixture
def store(_test_database):
    s = StateStore(_test_db_url())
    s.init_schema()
    s._conn.execute("TRUNCATE export_cursor, usage_rows, dead_letters, deliveries, event_acknowledgements, worker_status, reconciliation_runs")
    yield s
    s.close()


def make_row(ts: datetime = T0, row_id: str | None = None) -> UsageRow:
    return UsageRow(
        id=row_id or str(uuid.uuid4()),
        request_id="req-1",
        timestamp=ts,
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
    )


# --- Schema --------------------------------------------------------------

def test_init_schema_can_run_twice(store):
    store.init_schema()  # second time: must not fail or change anything


# --- Cursor --------------------------------------------------------------

def test_cursor_is_empty_on_first_run(store):
    assert store.get_cursor() is None


def test_cursor_moves_forward(store):
    first = Cursor(T0, str(uuid.uuid4()))
    later = Cursor(T0 + timedelta(seconds=1), str(uuid.uuid4()))
    assert store.advance_cursor(first)
    assert store.advance_cursor(later)
    assert store.get_cursor() == later


def test_cursor_never_moves_backwards(store):
    later = Cursor(T0 + timedelta(seconds=1), str(uuid.uuid4()))
    earlier = Cursor(T0, str(uuid.uuid4()))
    store.advance_cursor(later)
    assert not store.advance_cursor(earlier)
    assert store.get_cursor() == later


def test_same_timestamp_is_ordered_by_id(store):
    low = Cursor(T0, "00000000-0000-0000-0000-000000000001")
    high = Cursor(T0, "00000000-0000-0000-0000-000000000002")
    store.advance_cursor(low)
    assert store.advance_cursor(high)
    assert not store.advance_cursor(low)
    assert store.get_cursor() == high


# --- Usage rows ----------------------------------------------------------

def test_recording_a_row_twice_keeps_one_line(store):
    row = make_row()
    store.record_row(row, RowStatus.FAILED, "sub_acme", error="HTTP 503")
    store.record_row(row, RowStatus.SENT, "sub_acme", {TokenKind.INPUT: 35, TokenKind.OUTPUT: 5})

    count, status, attempts, out_tokens, error = store._conn.execute(
        "SELECT count(*), max(status), max(attempts), max(output_tokens_sent), max(last_error) "
        "FROM usage_rows"
    ).fetchone()
    assert (count, status, attempts, out_tokens, error) == (1, "sent", 2, 5, None)


def test_statuses_only_returns_known_rows(store):
    sent, unmapped, unseen = make_row(), make_row(), make_row()
    store.record_row(sent, RowStatus.SENT, "sub_acme")
    store.record_row(unmapped, RowStatus.UNMAPPED)
    assert store.statuses([sent.id, unmapped.id, unseen.id]) == {
        sent.id: RowStatus.SENT,
        unmapped.id: RowStatus.UNMAPPED,
    }


# --- Dead letters --------------------------------------------------------

def test_dead_letter_is_not_duplicated(store):
    row_id = str(uuid.uuid4())
    store.add_dead_letter(row_id, "unmapped", "no lago: label and no user_path")
    store.add_dead_letter(row_id, "unmapped", "still no mapping")
    open_letters = store.open_dead_letters()
    assert len(open_letters) == 1
    assert open_letters[0]["error"] == "still no mapping"


def test_resolved_dead_letter_disappears_from_open_list(store):
    row_id = str(uuid.uuid4())
    store.add_dead_letter(row_id, "unmapped")
    store.resolve_dead_letter(row_id)
    assert store.open_dead_letters() == []


# --- Transactions --------------------------------------------------------

def test_transaction_is_all_or_nothing(store):
    row = make_row()
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.record_row(row, RowStatus.SENT, "sub_acme")
            store.advance_cursor(Cursor(row.timestamp, row.id))
            raise RuntimeError("crash before commit")
    # Nothing was saved: no row, no cursor.
    assert store.statuses([row.id]) == {}
    assert store.get_cursor() is None


def test_status_counts(store):
    store.record_row(make_row(), RowStatus.SENT, "sub_acme")
    store.record_row(make_row(), RowStatus.NOT_BILLABLE)
    store.add_dead_letter(str(uuid.uuid4()), "unmapped")
    counts = store.status_counts()
    assert counts["sent"] == 1
    assert counts["not_billable"] == 1
    assert counts["failed"] == 0
    assert counts["dead_letters_open"] == 1