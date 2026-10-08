import json
from types import SimpleNamespace
from unittest.mock import Mock
import threading

from fastapi.testclient import TestClient
import pytest

from demo import live_app, live_reset


@pytest.fixture
def reset_data(tmp_path, monkeypatch):
    monkeypatch.setattr(live_reset, 'DATA', tmp_path)
    monkeypatch.setattr(live_app, 'RESET_DATA', tmp_path)
    monkeypatch.setenv('GOMODEL_ADMIN_DB_URL', 'postgresql://test')
    monkeypatch.setenv('READONLY_PASSWORD', 'test')
    history = live_app.History(tmp_path / 'history.sqlite')
    history.save({'id': 'old-request'})
    lab = SimpleNamespace(tasks=set(), guard=threading.RLock(), history=history,
                          control={'paused': False, 'network_fault': True})
    monkeypatch.setattr(live_app.app.state, 'lab', lab, raising=False)
    return tmp_path, lab


def test_reset_stops_writers_before_deleting_and_restores_configuration(reset_data):
    path, lab = reset_data
    docker = Mock()
    live_reset.reset(docker)
    calls = docker.mock_calls
    first_exec = next(i for i,c in enumerate(calls) if c[0] == 'execute')
    assert [c.args[0] for c in calls[:first_exec]] == [
        'live-lab', 'exporter', 'gomodel', 'lago-clock', 'lago-worker', 'lago-api', 'lago-migrate']
    commands = [c.args for c in calls if c[0] == 'execute']
    assert [c[0] for c in commands] == ['gomodel-db', 'exporter-db', 'lago-redis', 'lago-db', 'live-lab']
    assert 'TRUNCATE TABLE usage;' in commands[0][1]
    assert 'reconciliation_runs' in commands[1][1][-1]
    assert commands[2][1] == ['redis-cli', 'FLUSHALL']
    assert 'DROP DATABASE IF EXISTS lago WITH (FORCE);' in commands[3][1]
    assert commands[-1][1] == ['python', '-m', 'demo.live_setup']
    assert lab.history.recent() == []
    assert lab.history.controls() == {'paused': True, 'network_fault': False}
    assert json.loads((path/'reset-status.json').read_text())['state'] == 'complete'
    assert not any(c.args and c.args[0] == 'ollama' for c in calls)


def test_reset_never_deletes_if_a_writer_cannot_be_stopped(reset_data):
    path, lab = reset_data
    docker = Mock()
    docker.stop.side_effect = RuntimeError('writer still active')
    with pytest.raises(RuntimeError):
        live_reset.reset(docker)
    docker.execute.assert_not_called()
    assert len(lab.history.recent()) == 1


def test_reset_endpoint_requires_guard_idle_inference_and_live_helper(reset_data):
    path, lab = reset_data
    client = TestClient(live_app.app)
    headers = {'X-Lab-Action': '1'}
    assert client.post('/api/reset').status_code == 403
    assert client.post('/api/reset', headers={**headers, 'Origin': 'https://example.com'}).status_code == 403
    lab.tasks.add('inference')
    assert client.post('/api/reset', headers=headers).status_code == 409
    lab.tasks.clear()
    assert client.post('/api/reset', headers=headers).status_code == 503
    (path/'reset-heartbeat').touch()
    assert client.post('/api/reset', headers=headers).status_code == 202
    assert (path/'reset-request').exists()
    assert lab.control == {'paused': True, 'network_fault': False}
    assert client.post('/api/reset', headers=headers).status_code == 409
    assert client.post('/api/requests', headers=headers, json={}).status_code == 409
    assert client.get('/api/reset').json()['state'] == 'queued'
    assert len(lab.history.recent()) == 1  # Endpoint only queues; helper owns deletion.
