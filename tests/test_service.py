import time

from fastapi.testclient import TestClient
from typer.testing import CliRunner

import exporter.cli as cli
from exporter.lago_client import LagoClient
from exporter.reader import UsageReader
from exporter.reconcile import Reconciler
from exporter.state.store import StateStore
from tests.factories import T0, settings
from tests.test_runner import _databases, gomodel, state, insert, _url, STATE_DB, GOMODEL_DB
from tests.test_reconcile import lago


def test_serve_runs_workers_and_http_app_then_stops(gomodel, state, lago, monkeypatch):
    config = settings(poll_interval_seconds=0.05, reconciliation_lookback_days=1)
    open_store = lambda: StateStore(_url(STATE_DB))
    reader = UsageReader(_url(GOMODEL_DB))
    client = lago.client()
    reconciler = Reconciler(config, open_store, reader, client)
    monkeypatch.setattr(cli, '_components', lambda: (config, open_store, reader, client, reconciler))
    row = insert(gomodel, T0)
    def server(app, **kwargs):
        deadline = time.monotonic()+8
        while time.monotonic() < deadline:
            with open_store() as store:
                if row in store.statuses([row]):
                    break
            time.sleep(0.05)
        else:
            raise AssertionError('export worker never ran')
        with TestClient(app) as http:
            assert http.get('/').status_code == 200
            assert http.get('/metrics').status_code == 200
            assert http.get('/status').json()['workers']['export']
    monkeypatch.setattr('uvicorn.run', server)
    result = CliRunner().invoke(cli.app, ['serve'])
    assert result.exit_code == 0, result.exception
    import threading
    assert not any(t.name in ('export', 'reconcile') for t in threading.enumerate())
