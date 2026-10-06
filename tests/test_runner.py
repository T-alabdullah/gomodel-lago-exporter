"""End-to-end tests of one export cycle: GoModel table -> exporter -> fake Lago.

These prove the Thursday guarantees automatically:
  - no double billing (re-running, and crashing between send and save)
  - no lost usage (Lago down, then back)
  - late rows behind the cursor are still sent
  - unmapped and refused usage is visible in the dead-letter table

Needs the exporter's Postgres (`docker compose up -d`), like the other DB tests.
"""

import json
import os
import uuid
from datetime import timedelta

import psycopg
import pytest

from exporter.reader import UsageReader
from exporter.runner import Runner
from exporter.sender import Sender
from exporter.state.store import StateStore
from tests.factories import T0, settings
from tests.fake_lago import FakeLago
from tests.test_reader import GOMODEL_USAGE_TABLE

ADMIN_DSN = os.environ.get(
    "EXPORTER_TEST_ADMIN_DB_URL", "postgresql://postgres:exporter@localhost:5434/postgres"
)
STATE_DB, GOMODEL_DB = "runner_state_test", "runner_gomodel_test"


def _url(db: str) -> str:
    return ADMIN_DSN.rsplit("/", 1)[0] + "/" + db


@pytest.fixture(scope="session")
def _databases():
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True, connect_timeout=3) as conn:
            for db in (STATE_DB, GOMODEL_DB):
                if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db,)).fetchone():
                    conn.execute(f'CREATE DATABASE "{db}"')
    except psycopg.OperationalError as e:
        pytest.skip(f"exporter Postgres not reachable ({e.__class__.__name__}); run docker compose up -d")


@pytest.fixture
def gomodel(_databases):
    """A fresh GoModel-shaped usage table."""
    with psycopg.connect(_url(GOMODEL_DB), autocommit=True) as conn:
        conn.execute("DROP TABLE IF EXISTS usage")
        conn.execute(GOMODEL_USAGE_TABLE)
        yield conn


@pytest.fixture
def state(_databases):
    """A fresh, empty exporter state database."""
    with StateStore(_url(STATE_DB)) as s:
        s.init_schema()
        s._conn.execute("TRUNCATE export_cursor, usage_rows, dead_letters, deliveries, event_acknowledgements, worker_status, reconciliation_runs")
        yield s


@pytest.fixture
def lago():
    return FakeLago()


def make_runner(lago, store_class=StateStore, **overrides) -> Runner:
    s = settings(**overrides)
    return Runner(
        s,
        open_store=lambda: store_class(_url(STATE_DB)),
        reader=UsageReader(_url(GOMODEL_DB)),
        sender=Sender(lago.client(), s, sleep=lambda _: None),
    )


def insert(db, ts, *, key: str = "acme", provider: str = "ollama-qai") -> str:
    """One usage row like GoModel writes it, for one of the Step 2 test keys."""
    labels, user_path = {
        "acme": (["lago:sub_acme"], "/"),
        "beta": (None, "/customers/sub_beta"),
        "ghost": (None, "/"),
    }[key]
    row_id = str(uuid.uuid4())
    db.execute(
        """INSERT INTO usage (id, request_id, provider_id, timestamp, model, provider,
                              provider_name, endpoint, user_path, input_tokens,
                              output_tokens, total_tokens, labels)
           VALUES (%s, 'req', 'p', %s, 'qwen2.5:0.5b', 'ollama', %s, '/v1/chat/completions',
                   %s, 35, 5, 40, %s)""",
        (row_id, ts, provider, user_path, json.dumps(labels) if labels else None),
    )
    return row_id


def status_of(state, row_id):
    return state.statuses([row_id]).get(row_id)


# --- Every kind of row ends up in the right place ------------------------

def test_first_cycle_handles_every_kind_of_row(gomodel, state, lago):
    acme = insert(gomodel, T0, key="acme")
    beta = insert(gomodel, T0 + timedelta(seconds=1), key="beta")
    ghost = insert(gomodel, T0 + timedelta(seconds=2), key="ghost")
    byok = insert(gomodel, T0 + timedelta(seconds=3), key="acme", provider="ollama-ext")

    report = make_runner(lago).run_cycle()

    assert [status_of(state, r).value for r in (acme, beta, ghost, byok)] == \
        ["sent", "sent", "unmapped", "not_billable"]
    assert len(lago.stored) == 4                              # 2 rows x (in + out)
    assert [d["usage_row_id"] for d in state.open_dead_letters()] == [ghost]
    assert state.get_cursor().row_id == byok                  # moved to the last row
    assert (report.sent, report.unmapped, report.not_billable) == (2, 1, 1)


def test_tokens_sent_are_recorded_for_reconciliation(gomodel, state, lago):
    insert(gomodel, T0, key="acme")
    make_runner(lago).run_cycle()
    sub, tokens_in, tokens_out = state._conn.execute(
        "SELECT external_subscription_id, input_tokens_sent, output_tokens_sent FROM usage_rows"
    ).fetchone()
    assert (sub, tokens_in, tokens_out) == ("sub_acme", 35, 5)


# --- No double billing ---------------------------------------------------

def test_running_again_sends_nothing_twice(gomodel, state, lago):
    for i in range(5):
        insert(gomodel, T0 + timedelta(seconds=i))
    runner = make_runner(lago)
    runner.run_cycle()
    posts_after_first = lago.count("POST", "/api/v1/events/batch")
    report = runner.run_cycle()
    assert lago.count("POST", "/api/v1/events/batch") == posts_after_first
    assert report.rows_skipped == 5 and len(lago.stored) == 10


class CrashingStore(StateStore):
    """Crashes right after Lago accepted the events, before anything is saved."""

    def advance_cursor(self, cursor):
        raise RuntimeError("power cut!")


def test_crash_between_send_and_save_never_double_bills(gomodel, state, lago):
    row = insert(gomodel, T0)
    with pytest.raises(RuntimeError):
        make_runner(lago, store_class=CrashingStore).run_cycle()
    assert len(lago.stored) == 2          # Lago got the events...
    assert status_of(state, row) is None  # ...but the exporter saved nothing (rolled back)

    make_runner(lago).run_cycle()         # restart
    assert len(lago.stored) == 2          # Lago said "duplicate": nothing billed twice
    assert status_of(state, row).value == "sent"


# --- No lost usage -------------------------------------------------------

def test_lago_outage_loses_nothing(gomodel, state, lago):
    rows = [insert(gomodel, T0 + timedelta(seconds=i)) for i in range(3)]
    lago.fail_next = ["network"] * 100     # Lago is down
    report = make_runner(lago, max_retries=2).run_cycle()
    assert report.waiting_for_retry == 3
    assert state.get_cursor() is None      # cursor did not move past unsent usage
    assert all(status_of(state, r) is None for r in rows)
    assert state.open_dead_letters() == [] # temporary trouble is not a dead letter

    lago.fail_next = []                    # Lago is back
    make_runner(lago).run_cycle()
    assert all(status_of(state, r).value == "sent" for r in rows)
    assert len(lago.stored) == 6


# --- Late rows -----------------------------------------------------------

def test_late_row_behind_cursor_is_still_sent(gomodel, state, lago):
    insert(gomodel, T0)
    newest = insert(gomodel, T0 + timedelta(minutes=5))
    runner = make_runner(lago)
    runner.run_cycle()
    assert state.get_cursor().row_id == newest

    late = insert(gomodel, T0 + timedelta(minutes=3))   # lands behind the cursor
    runner.run_cycle()
    assert status_of(state, late).value == "sent"
    assert state.get_cursor().row_id == newest          # cursor never moves backwards


def test_reads_in_pages(gomodel, state, lago):
    rows = [insert(gomodel, T0 + timedelta(seconds=i)) for i in range(7)]
    make_runner(lago, read_batch_size=2).run_cycle()
    assert all(status_of(state, r).value == "sent" for r in rows)


# --- Problems are visible, never silent ----------------------------------

def test_usage_before_subscription_start_goes_to_dead_letter(gomodel, state, lago):
    lago.subscriptions["sub_acme"] = T0 + timedelta(hours=1)
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    assert status_of(state, row).value == "failed"
    letter = state.open_dead_letters()[0]
    assert letter["reason"] == "rejected" and "before subscription" in letter["error"]
    assert state.get_cursor().row_id == row             # a permanent problem doesn't block others


def test_fixed_problem_clears_its_dead_letter(gomodel, state, lago):
    lago.subscriptions.pop("sub_acme")                   # subscription missing...
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    assert len(state.open_dead_letters()) == 1
    state._conn.execute("DELETE FROM usage_rows")        # e.g. a backfill replays the row
    lago.subscriptions["sub_acme"] = T0 - timedelta(days=1)   # ...then created
    make_runner(lago).run_cycle()
    assert status_of(state, row).value == "sent"
    assert state.open_dead_letters() == []