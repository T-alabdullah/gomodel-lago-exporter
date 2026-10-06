"""Replay and failure regressions using real PostgreSQL and the HTTP Lago fake."""

import json
from datetime import timedelta

import pytest

from exporter.contracts import RowStatus, TokenKind
from exporter.state.store import ExporterBusyError, StateStore
from tests.factories import T0
from tests.fake_lago import BAD_CODE
from tests.test_runner import (
    _databases, gomodel, state, lago, make_runner, insert, status_of,
    CrashingStore, _url, STATE_DB,
)

END = T0 + timedelta(days=1)


def totals(lago):
    return {
        key: value.copy() for key, value in lago.stored.items()
    }


def test_backfill_twice_keeps_identical_lago_totals_and_cursor(gomodel, state, lago):
    rows = [insert(gomodel, T0 + timedelta(seconds=i)) for i in range(7)]
    runner = make_runner(lago, read_batch_size=2)
    first = runner.backfill(T0, END)
    expected = totals(lago)
    second = runner.backfill(T0, END)
    assert first.sent == second.sent == 7
    assert len(expected) == 14 and totals(lago) == expected
    assert state.get_cursor() is None
    assert all(status_of(state, row) is RowStatus.SENT for row in rows)


def test_backfill_boundaries_include_start_and_exclude_end(gomodel, state, lago):
    before = insert(gomodel, T0 - timedelta(microseconds=1))
    first = insert(gomodel, T0)
    last = insert(gomodel, END - timedelta(microseconds=1))
    after = insert(gomodel, END)
    make_runner(lago, read_batch_size=1).backfill(T0, END)
    assert set(state.statuses([before, first, last, after])) == {first, last}


def test_zero_uuid_at_start_is_not_skipped(gomodel, state, lago):
    row = insert(gomodel, T0)
    zero = '00000000-0000-0000-0000-000000000000'
    gomodel.execute('UPDATE usage SET id = %s WHERE id = %s', (zero, row))
    make_runner(lago).backfill(T0, END)
    assert status_of(state, zero) is RowStatus.SENT


def test_backfill_does_not_advance_existing_poll_cursor(gomodel, state, lago):
    first = insert(gomodel, T0)
    runner = make_runner(lago)
    runner.run_cycle()
    cursor = state.get_cursor()
    insert(gomodel, END)
    runner.backfill(T0, END + timedelta(days=1))
    assert state.get_cursor() == cursor and cursor.row_id == first


def test_backfill_recovers_unmapped_rows_and_closes_dead_letter(gomodel, state, lago):
    row = insert(gomodel, T0, key='ghost')
    runner = make_runner(lago)
    runner.run_cycle()
    assert status_of(state, row) is RowStatus.UNMAPPED
    gomodel.execute('UPDATE usage SET labels = %s WHERE id = %s', (json.dumps(['lago:sub_acme']), row))
    runner.backfill(T0, END)
    assert status_of(state, row) is RowStatus.SENT
    assert state.open_dead_letters() == []


def test_backfill_recovers_rejected_subscription(gomodel, state, lago):
    row = insert(gomodel, T0)
    lago.subscriptions.pop('sub_acme')
    make_runner(lago).run_cycle()
    lago.subscriptions['sub_acme'] = T0
    make_runner(lago).backfill(T0, END)
    assert status_of(state, row) is RowStatus.SENT
    assert state.open_dead_letters() == []


def test_crash_then_mapping_and_config_change_cannot_double_bill(gomodel, state, lago):
    row = insert(gomodel, T0)
    with pytest.raises(RuntimeError, match='power cut'):
        make_runner(lago, store_class=CrashingStore).run_cycle()
    expected = totals(lago)
    assert len(state.pending_deliveries(10)) == 1
    gomodel.execute('UPDATE usage SET labels = %s, input_tokens = 999, raw_data = %s WHERE id = %s',
                    (json.dumps(['lago:sub_beta']), json.dumps({'cached_tokens': 500}), row))
    make_runner(lago, metric_input='changed_metric', billable_providers=[]).run_cycle()
    assert totals(lago) == expected
    assert state.pending_deliveries(10) == []
    assert state.get_delivery(row).subscription_id == 'sub_acme'
    assert status_of(state, row) is RowStatus.SENT


def test_pending_delivery_survives_source_deletion(gomodel, state, lago):
    row = insert(gomodel, T0)
    lago.fail_next = [503] * 100
    assert make_runner(lago, max_retries=0).run_cycle().waiting_for_retry == 1
    gomodel.execute('DELETE FROM usage WHERE id = %s', (row,))
    lago.fail_next = []
    make_runner(lago).run_cycle()
    assert status_of(state, row) is RowStatus.SENT
    assert len(lago.stored) == 2


def test_partial_send_then_retry_preserves_original_subscription(gomodel, state, lago):
    row = insert(gomodel, T0)
    original = lago._batch
    calls = 0

    def fail_second_batch(events):
        nonlocal calls
        calls += 1
        if calls == 2:
            import httpx
            return httpx.Response(503, json={})
        return original(events)

    lago._batch = fail_second_batch
    report = make_runner(lago, lago_batch_size=1, max_retries=0).run_cycle()
    assert report.waiting_for_retry == 1 and len(lago.stored) == 1
    assert state.acknowledged_tokens(row) == {TokenKind.INPUT: 35}
    gomodel.execute('UPDATE usage SET labels = %s WHERE id = %s', (json.dumps(['lago:sub_beta']), row))
    make_runner(lago).run_cycle()
    assert len(lago.stored) == 2
    assert {key[0] for key in lago.stored} == {'sub_acme'}
    assert state.acknowledged_tokens(row) == {TokenKind.INPUT: 35, TokenKind.OUTPUT: 5}


def test_partial_permanent_failure_keeps_accepted_counts_on_replay(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago, metric_output=BAD_CODE).run_cycle()
    assert status_of(state, row) is RowStatus.FAILED
    assert state.acknowledged_tokens(row) == {TokenKind.INPUT: 35}
    lago.subscriptions.pop('sub_acme')
    make_runner(lago).backfill(T0, END)
    assert state._conn.execute('SELECT input_tokens_sent FROM usage_rows').fetchone()[0] == 35


def test_legacy_sent_row_reuses_subscription_and_saved_counts(gomodel, state, lago):
    row_id = insert(gomodel, T0)
    runner = make_runner(lago)
    runner.run_cycle()
    expected = totals(lago)
    state._conn.execute('DELETE FROM deliveries')  # Simulate state produced by the handover version.
    gomodel.execute('UPDATE usage SET labels = %s, input_tokens = 999, raw_data = %s WHERE id = %s',
                    (json.dumps(['lago:sub_beta']), json.dumps({'cached_tokens': 500}), row_id))
    runner.backfill(T0, END)
    assert totals(lago) == expected
    assert state.acknowledged_tokens(row_id) == {TokenKind.INPUT: 35, TokenKind.OUTPUT: 5}


def test_late_retry_is_recovered_even_outside_overlap(gomodel, state, lago):
    insert(gomodel, END)
    make_runner(lago).run_cycle()
    late = insert(gomodel, END - timedelta(minutes=9))
    lago.fail_next = [503] * 100
    make_runner(lago, max_retries=0).run_cycle()
    # Later cursor movement/config change must not orphan the discovered late row.
    lago.fail_next = []
    make_runner(lago, overlap_window_seconds=0).run_cycle()
    assert status_of(state, late) is RowStatus.SENT


def test_backfill_outage_stops_and_same_range_can_resume(gomodel, state, lago):
    rows = [insert(gomodel, T0 + timedelta(seconds=i)) for i in range(4)]
    lago.fail_next = [503] * 100
    assert make_runner(lago, max_retries=0, read_batch_size=2).backfill(T0, END).waiting_for_retry == 2
    lago.fail_next = []
    make_runner(lago, read_batch_size=2).backfill(T0, END)
    assert len(lago.stored) == 8
    assert all(status_of(state, row) is RowStatus.SENT for row in rows)
    assert state.get_cursor() is None


def test_concurrent_writers_fail_before_sending(gomodel, state, lago):
    insert(gomodel, T0)
    with state.writer_lock():
        with pytest.raises(ExporterBusyError):
            make_runner(lago).backfill(T0, END)
        assert not lago.requests
    assert make_runner(lago).backfill(T0, END).sent == 1


def test_lock_releases_after_crash(gomodel, state, lago):
    insert(gomodel, T0)
    with pytest.raises(RuntimeError):
        make_runner(lago, store_class=CrashingStore).run_cycle()
    assert make_runner(lago).run_cycle().sent == 1


def test_single_row_replay_uses_durable_state(gomodel, state, lago):
    row = insert(gomodel, T0)
    runner = make_runner(lago)
    source = runner._reader.find_by_id_prefix(row)[0]
    runner.send_row(source)
    before = totals(lago)
    runner.send_row(source)
    assert totals(lago) == before and status_of(state, row) is RowStatus.SENT
    assert state.get_cursor() is None


def test_empty_backfill_is_successful_without_advancing(gomodel, state, lago):
    report = make_runner(lago).backfill(T0, END)
    assert report.rows_read == 0 and state.get_cursor() is None
    assert not lago.requests


@pytest.mark.parametrize('start,end', [(END, T0), (T0, T0), (T0.replace(tzinfo=None), END)])
def test_invalid_ranges_rejected_before_work(lago, start, end):
    with pytest.raises(ValueError):
        make_runner(lago).backfill(start, end)
    assert not lago.requests


def test_legacy_accepted_counts_survive_rejected_replay(gomodel, state, lago):
    row = insert(gomodel, T0)
    make_runner(lago).run_cycle()
    state._conn.execute('DELETE FROM deliveries')
    lago.subscriptions.pop('sub_acme')
    make_runner(lago).backfill(T0, END)
    assert status_of(state, row) is RowStatus.FAILED
    assert state.acknowledged_tokens(row) == {TokenKind.INPUT: 35, TokenKind.OUTPUT: 5}
    assert state._conn.execute('SELECT input_tokens_sent, output_tokens_sent FROM usage_rows').fetchone() == (35, 5)


def test_explicit_exclusion_closes_previously_unmapped_dead_letter(gomodel, state, lago):
    row = insert(gomodel, T0, key='ghost')
    make_runner(lago).run_cycle()
    assert state.open_dead_letters()
    make_runner(lago, billable_providers=[]).backfill(T0, END)
    assert status_of(state, row) is RowStatus.NOT_BILLABLE
    assert not state.open_dead_letters()
    assert not lago.stored
