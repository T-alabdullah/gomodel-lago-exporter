from datetime import timedelta

from exporter.scheduler import DailyScheduler
from exporter.state.store import StateStore
from tests.factories import settings
from tests.test_runner import _databases, gomodel, state, _url, STATE_DB
from tests.test_reconcile import lago, make_reconciler, DAY, END, NOW


def scheduler(lago, now=NOW, **overrides):
    config = settings(reconciliation_lookback_days=1, **overrides)
    return DailyScheduler(config, lambda: StateStore(_url(STATE_DB)), make_reconciler(lago), now=lambda: now)


def test_scheduler_restart_does_not_duplicate_completed_day(gomodel, state, lago):
    first = scheduler(lago).tick()
    assert first['start'] == DAY.isoformat()
    assert first['status'] == 'matched'
    state._conn.execute('UPDATE reconciliation_runs SET completed_at = %s', (NOW,))
    assert scheduler(lago).tick() is None
    assert state._conn.execute('SELECT count(*) FROM reconciliation_runs').fetchone()[0] == 1


def test_delay_waits_until_day_is_settled(gomodel, state, lago):
    report = scheduler(lago, now=END+timedelta(minutes=5)).tick()
    assert report['end'] == DAY.isoformat()


def test_incomplete_day_retries_after_backoff(gomodel, state, lago):
    lago.read_failure = 503
    first = scheduler(lago).tick()
    assert first['status'] == 'incomplete'
    state._conn.execute('UPDATE reconciliation_runs SET completed_at = %s', (NOW,))
    lago.read_failure = None
    assert scheduler(lago).tick() is None
    report = scheduler(lago, now=NOW+timedelta(minutes=6)).tick()
    assert report['status'] == 'matched'


def test_due_check_under_lock_prevents_duplicate_schedulers(gomodel, state, lago):
    reconciler = make_reconciler(lago)
    reconciler.run_if_due(DAY, END, NOW)
    state._conn.execute('UPDATE reconciliation_runs SET completed_at = %s', (NOW,))
    assert reconciler.run_if_due(DAY, END, NOW) is None


def test_catchup_uses_configured_lookback(gomodel, state, lago):
    config = settings(reconciliation_lookback_days=3)
    task = DailyScheduler(config, lambda: StateStore(_url(STATE_DB)), make_reconciler(lago), now=lambda: NOW)
    starts = []
    for _ in range(3):
        starts.append(task.tick()['start'])
        state._conn.execute('UPDATE reconciliation_runs SET completed_at = %s', (NOW,))
    assert starts == [(DAY-timedelta(days=i)).isoformat() for i in range(3)]
    assert task.tick() is None


def test_persistent_mismatch_does_not_starve_unaudited_day(gomodel, state, lago):
    state.save_reconciliation(dict(start=DAY.isoformat(), end=END.isoformat(), status='mismatch', issues=[]))
    state._conn.execute('UPDATE reconciliation_runs SET completed_at = %s', (NOW-timedelta(hours=1),))
    task = DailyScheduler(settings(reconciliation_lookback_days=2), lambda: StateStore(_url(STATE_DB)),
                          make_reconciler(lago), now=lambda: NOW)
    assert task.tick()['end'] == DAY.isoformat()
