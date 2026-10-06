"""Tests for the reader, against a copy of GoModel's usage table.

Like the state tests, these need the exporter's Postgres (`docker compose up -d`)
and use their own database, `gomodel_test`, with a usage table shaped exactly
like GoModel's (copied from GoModel 0.1.99, internal/usage/store_postgresql.go).
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from exporter.reader import MIN_ID, UsageReader, overlap_start
from exporter.state.store import Cursor

ADMIN_DSN = os.environ.get(
    "EXPORTER_TEST_ADMIN_DB_URL", "postgresql://postgres:exporter@localhost:5434/postgres"
)
TEST_DB = "gomodel_test"
T0 = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)

GOMODEL_USAGE_TABLE = """
CREATE TABLE usage (
    id UUID PRIMARY KEY,
    request_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    model TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_name TEXT,
    endpoint TEXT NOT NULL,
    user_path TEXT,
    session_id TEXT,
    cache_type TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    rewrite_tokens_saved INTEGER NOT NULL DEFAULT 0,
    rewrite_cost_saved DOUBLE PRECISION,
    raw_data JSONB,
    input_cost DOUBLE PRECISION,
    output_cost DOUBLE PRECISION,
    total_cost DOUBLE PRECISION,
    cost_source TEXT DEFAULT '',
    costs_calculation_caveat TEXT DEFAULT '',
    labels JSONB
)
"""


def _test_db_url() -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/" + TEST_DB


@pytest.fixture(scope="session")
def _test_database():
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3) as conn:
            if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (TEST_DB,)).fetchone():
                conn.execute(f'CREATE DATABASE "{TEST_DB}"')
    except psycopg.OperationalError as e:
        pytest.skip(f"exporter Postgres not reachable ({e.__class__.__name__}); run docker compose up -d")


@pytest.fixture
def db(_test_database):
    """A fresh, empty GoModel-shaped usage table for each test."""
    with psycopg.connect(_test_db_url(), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS usage")
        conn.execute(GOMODEL_USAGE_TABLE)
        yield conn


@pytest.fixture
def reader(db):
    return UsageReader(_test_db_url())


def insert(db, ts: datetime, row_id: str | None = None, **overrides) -> str:
    """Insert one usage row like GoModel would, return its id."""
    row = {
        "id": row_id or str(uuid.uuid4()),
        "request_id": "req-" + uuid.uuid4().hex[:8],
        "provider_id": "chatcmpl-x",
        "timestamp": ts,
        "model": "qwen2.5:0.5b",
        "provider": "ollama",
        "provider_name": "ollama-qai",
        "endpoint": "/v1/chat/completions",
        "user_path": "/",
        "cache_type": None,
        "input_tokens": 35,
        "output_tokens": 5,
        "total_tokens": 40,
        "raw_data": None,
        "labels": None,
    } | overrides
    for key in ("raw_data", "labels"):
        if row[key] is not None:
            row[key] = json.dumps(row[key])
    cols = ", ".join(row)
    values = ", ".join(f"%({c})s" for c in row)
    db.execute(f"INSERT INTO usage ({cols}) VALUES ({values})", row)
    return row["id"]


# --- Order and paging ----------------------------------------------------

def test_reads_oldest_first(db, reader):
    late = insert(db, T0 + timedelta(seconds=2))
    early = insert(db, T0)
    middle = insert(db, T0 + timedelta(seconds=1))
    assert [r.id for r in reader.read_after(None, 10)] == [early, middle, late]


def test_read_after_starts_strictly_after_the_position(db, reader):
    first = insert(db, T0)
    second = insert(db, T0 + timedelta(seconds=1))
    rows = reader.read_after(Cursor(T0, first), 10)
    assert [r.id for r in rows] == [second]


def test_paging_one_by_one_never_skips_or_repeats(db, reader):
    # Five rows, three sharing the exact same timestamp: the tricky case.
    ids = [
        insert(db, T0, "00000000-0000-0000-0000-00000000000c"),
        insert(db, T0, "00000000-0000-0000-0000-00000000000a"),
        insert(db, T0, "00000000-0000-0000-0000-00000000000b"),
        insert(db, T0 + timedelta(seconds=1)),
        insert(db, T0 + timedelta(seconds=2)),
    ]
    seen, position = [], None
    while batch := reader.read_after(position, limit=1):
        seen.append(batch[0].id)
        position = Cursor(batch[0].timestamp, batch[0].id)
    assert sorted(seen[:3]) == seen[:3]          # same timestamp -> ordered by id
    assert sorted(seen) == sorted(ids) and len(seen) == len(set(seen)) == 5


def test_until_stops_before_that_time(db, reader):
    inside = insert(db, T0)
    insert(db, T0 + timedelta(hours=1))
    rows = reader.read_after(None, 10, until=T0 + timedelta(minutes=30))
    assert [r.id for r in rows] == [inside]


# --- Late rows -----------------------------------------------------------

def test_late_row_behind_cursor_is_found_by_overlap_window(db, reader):
    insert(db, T0)
    newest = insert(db, T0 + timedelta(minutes=5))
    cursor = Cursor(T0 + timedelta(minutes=5), newest)

    # A row lands late: its timestamp is BEHIND the cursor.
    late = insert(db, T0 + timedelta(minutes=3))

    assert reader.read_after(cursor, 10) == []                       # reading forward misses it
    start = overlap_start(cursor, overlap_seconds=600)                # 10 minutes back
    assert late in [r.id for r in reader.read_after(start, 10)]      # the overlap finds it


def test_overlap_start_on_first_run_is_the_beginning():
    assert overlap_start(None, 600) is None


def test_overlap_start_goes_back_from_cursor():
    start = overlap_start(Cursor(T0, str(uuid.uuid4())), 600)
    assert start == Cursor(T0 - timedelta(minutes=10), MIN_ID)


# --- Converting rows -----------------------------------------------------

def test_row_fields_are_converted(db, reader):
    row_id = insert(
        db, T0,
        labels=["lago:sub_acme", "team:x"],
        raw_data={"cached_tokens": 12},
        cache_type="exact",
        user_path="/customers/sub_beta",
    )
    row = reader.read_after(None, 1)[0]
    assert row.id == row_id
    assert row.timestamp == T0
    assert row.labels == ("lago:sub_acme", "team:x")
    assert row.raw_data == {"cached_tokens": 12}
    assert row.cache_type == "exact" and row.is_cache_hit
    assert row.user_path == "/customers/sub_beta"
    assert (row.input_tokens, row.output_tokens) == (35, 5)


def test_null_and_empty_fields_get_safe_defaults(db, reader):
    insert(db, T0, labels=None, raw_data=None, cache_type="", provider_name=None)
    row = reader.read_after(None, 1)[0]
    assert row.labels == ()
    assert row.raw_data == {}
    assert row.cache_type is None and not row.is_cache_hit
    assert row.provider_name is None

def test_reader_works_with_role_that_cannot_write(db):
    from psycopg import sql
    from psycopg.conninfo import make_conninfo
    role = 'test_reader_' + uuid.uuid4().hex[:12]
    insert(db, T0)
    db.execute(sql.SQL('CREATE ROLE {} LOGIN').format(sql.Identifier(role)))
    try:
        db.execute(sql.SQL('ALTER ROLE {} SET default_transaction_read_only = on').format(sql.Identifier(role)))
        db.execute(sql.SQL('GRANT USAGE ON SCHEMA public TO {}').format(sql.Identifier(role)))
        db.execute(sql.SQL('GRANT SELECT ON usage TO {}').format(sql.Identifier(role)))
        # SET ROLE on the existing administrative connection tests table grants;
        # a separate login would require changing the CI server's authentication.
        db.execute(sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        assert len(db.execute('SELECT * FROM usage').fetchall()) == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            db.execute('DELETE FROM usage')
    finally:
        db.execute('RESET ROLE')
        db.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
        db.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))
