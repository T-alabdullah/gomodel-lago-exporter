from datetime import timedelta

from fastapi.testclient import TestClient

from exporter.monitoring import Monitor, create_app
from exporter.reader import UsageReader
from exporter.state.store import StateStore
from tests.factories import T0, settings
from tests.test_runner import _databases, gomodel, state, insert, make_runner, _url, STATE_DB, GOMODEL_DB
from tests.test_reconcile import lago, make_reconciler, DAY, END, NOW


def monitor(**overrides):
    return Monitor(settings(**overrides), lambda: StateStore(_url(STATE_DB)),
                   UsageReader(_url(GOMODEL_DB)), now=lambda: NOW)


def fresh_worker(state):
    state._conn.execute("UPDATE worker_status SET started_at = %s, finished_at = %s", (NOW, NOW))
    state._conn.execute('UPDATE reconciliation_runs SET completed_at = %s', (NOW,))


def ready(gomodel, state, lago):
    insert(gomodel, T0)
    make_runner(lago).run_cycle()
    assert make_reconciler(lago).run(DAY, END)['status'] == 'matched'
    fresh_worker(state)


def test_idle_cursor_lag_does_not_raise_billing_lag(gomodel, state, lago):
    ready(gomodel, state, lago)
    snapshot = monitor().snapshot()
    assert snapshot['cursor_lag_seconds'] > 900
    assert snapshot['oldest_outstanding_age_seconds'] == 0
    assert snapshot['healthy']


def test_health_json_metrics_and_page(gomodel, state, lago):
    ready(gomodel, state, lago)
    with TestClient(create_app(monitor())) as client:
        assert client.get('/health').status_code == 200
        status = client.get('/status').json()
        assert status['reconciliation']['status'] == 'matched'
        metrics = client.get('/metrics')
        assert metrics.status_code == 200
        assert 'exporter_healthy 1.0' in metrics.text
        assert 'exporter_reconciliation_status 1.0' in metrics.text
        page = client.get('/')
        assert page.status_code == 200
        for phrase in ['GoModel', 'Cursor lag', 'Last completed run', 'Events acknowledged today', 'Open dead letters', 'Latest reconciliation']:
            assert phrase in page.text


def test_unmapped_usage_is_visible_and_html_escaped(gomodel, state, lago):
    insert(gomodel, T0, key='ghost')
    make_runner(lago).run_cycle()
    state._conn.execute("UPDATE dead_letters SET error = '<script>alert(1)</script>'")
    fresh_worker(state)
    with TestClient(create_app(monitor())) as client:
        page = client.get('/').text
        assert '&lt;script&gt;' in page and '<script>' not in page
        assert 'unmapped' in page
        assert client.get('/health').status_code == 503
        assert 'dead_letters' in client.get('/status').json()['alerts']


def test_backlog_alerts_even_when_cursor_is_current(gomodel, state, lago):
    ready(gomodel, state, lago)
    insert(gomodel, T0-timedelta(minutes=1))
    snapshot = monitor().snapshot()
    assert snapshot['backlog_rows'] == 1
    assert 'billing_lag' in snapshot['alerts']


def test_pending_usage_outside_source_window_alerts(gomodel, state, lago):
    row = insert(gomodel, T0)
    lago.read_failure = 503
    make_runner(lago, max_retries=0).run_cycle()
    gomodel.execute('DELETE FROM usage WHERE id = %s', (row,))
    snapshot = monitor().snapshot()
    assert snapshot['pending'] == 1
    assert 'billing_lag' in snapshot['alerts']


def test_stale_worker_is_unhealthy(gomodel, state, lago):
    ready(gomodel, state, lago)
    state._conn.execute("UPDATE worker_status SET finished_at = %s WHERE name = 'export'", (NOW-timedelta(hours=1),))
    assert 'exporter_stale' in monitor().snapshot()['alerts']


def test_duplicate_replay_does_not_increment_today_count(gomodel, state, lago):
    insert(gomodel, T0)
    runner = make_runner(lago)
    runner.run_cycle()
    before = state.monitoring_snapshot(NOW)['events_acknowledged_today']
    runner.backfill(DAY, END)
    assert state.monitoring_snapshot(NOW)['events_acknowledged_today'] == before == 2


def test_worker_failure_is_persisted_without_connection_secret(gomodel, state, lago):
    runner = make_runner(lago)
    def fail(*args, **kwargs):
        raise RuntimeError('postgres://secret-password')
    runner._reader.read_after = fail
    import pytest
    with pytest.raises(RuntimeError):
        runner.run_cycle()
    fresh_worker(state)
    snapshot = monitor().snapshot()
    assert snapshot['workers']['export']['succeeded'] is False
    assert snapshot['workers']['export']['error'] == 'RuntimeError'
    assert 'exporter_error' in snapshot['alerts']


def test_scan_cap_is_visible_not_healthy(gomodel, state, lago):
    for _ in range(3):
        insert(gomodel, T0)
    snapshot = monitor(monitor_max_rows=1).snapshot()
    assert snapshot['backlog_scan_incomplete']
    assert not snapshot['healthy']


def test_database_failure_returns_safe_503():
    def fail():
        raise RuntimeError('private-password')
    app = create_app(Monitor(settings(), fail, None, now=lambda: NOW))
    with TestClient(app) as client:
        assert client.get('/health').status_code == 503
        assert 'private-password' not in client.get('/status').text
        assert 'exporter_database_available 0.0' in client.get('/metrics').text


def test_latest_report_is_newest_period_not_last_catchup(gomodel, state, lago):
    state.save_reconciliation(dict(start=DAY.isoformat(), end=END.isoformat(), status='mismatch', issues=[]))
    state.save_reconciliation(dict(start=(DAY-timedelta(days=1)).isoformat(), end=DAY.isoformat(), status='matched', issues=[]))
    assert state.latest_reconciliation()['status'] == 'mismatch'
