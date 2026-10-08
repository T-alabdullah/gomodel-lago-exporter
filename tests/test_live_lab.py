"""The lab must preserve identity, fail visibly, and never expose API credentials."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from demo import live_app


def test_history_survives_restart_and_controls_are_durable(tmp_path):
    path = tmp_path / 'journal.sqlite'
    first = live_app.History(path)
    assert first.controls()['paused'] is True
    first.save({'id': 'one', 'answer': 'hello'})
    first.controls({'paused': False, 'network_fault': True})
    second = live_app.History(path)
    assert second.get('one')['answer'] == 'hello'
    assert second.controls() == {'paused': False, 'network_fault': True}
    second.save({'id': 'one', 'answer': 'hello again'})
    assert len(second.recent()) == 1


@pytest.mark.parametrize('customer,expected', [('acme', 'lago:sub_acme'), ('beta', None), ('ghost', None)])
@pytest.mark.parametrize('complete', [True, False])
def test_real_stream_contract_and_per_request_identity(tmp_path, monkeypatch, customer, expected, complete):
    monkeypatch.setenv('LIVE_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('GOMODEL_MASTER_KEY', 'master-test-secret')
    lab = live_app.Lab()
    captured = []
    def handler(request):
        captured.append(request)
        if request.url.path == '/admin/auth-keys':
            body = json.loads(request.content)
            assert body['labels'][0] == 'trace:unique-request'
            assert ('lago:sub_acme' in body['labels']) == (expected is not None)
            assert body.get('user_path') == ('/customers/sub_beta' if customer == 'beta' else None)
            return httpx.Response(200, json={'value': 'one-time-secret'})
        assert request.headers['authorization'] == 'Bearer one-time-secret'
        assert json.loads(request.content)['model'] == 'ollama-qai/llama3.2:1b'
        chunks = 'data: {"id":"response-a","choices":[{"delta":{"content":"Actual streamed text"}}]}\n\n'
        chunks += 'data: {"usage":{"prompt_tokens":20,"completion_tokens":3},"choices":[]}\n\n'
        if complete:
            chunks += 'data: [DONE]\n\n'
        return httpx.Response(200, text=chunks)
    original = httpx.AsyncClient
    monkeypatch.setattr(live_app.httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    record = {'id': 'unique-request', 'customer': customer, 'answer': '',
              'http_body': {'model': 'ollama-qai/llama3.2:1b'}}
    asyncio.run(lab.generate(record))
    saved = lab.history.get(record['id'])
    assert saved['answer'] == 'Actual streamed text'
    assert saved['status'] == ('completed' if complete else 'error')
    assert 'one-time-secret' not in json.dumps(saved)
    assert 'master-test-secret' not in json.dumps(saved)
    assert len(captured) == 2


def test_security_and_invalid_database_queries():
    # No lifespan is needed: rejected operations never touch a database.
    client = TestClient(live_app.app)
    assert client.get('/api/ping', headers={'host': 'attacker.example'}).status_code == 400
    assert client.post('/api/control', json={'paused': False, 'network_fault': False}).status_code == 403
    assert client.post('/api/control', headers={'X-Lab-Action':'1', 'Origin':'https://attacker.example'},
                       json={'paused': False, 'network_fault': False}).status_code == 403
    assert client.get('/api/database/state/pg_authid').status_code == 404
    assert client.get('/api/database/source/deliveries').status_code == 404
    assert client.get('/api/requests/not-a-uuid').status_code == 422


def test_settings_never_return_connection_secrets(monkeypatch):
    cfg = live_app.get_settings().model_copy(update={'gomodel_db_url': 'postgresql://u:top-secret@db/test',
        'state_db_url':'postgresql://u:other-secret@db/test'})
    live_app.app.state.lab = SimpleNamespace(settings=cfg, control={'paused':True,'network_fault':False})
    response = TestClient(live_app.app).get('/api/settings')
    assert response.status_code == 200
    assert 'top-secret' not in response.text and 'other-secret' not in response.text
    assert response.json()['source_database']['role'] == 'exporter_ro'


def test_prepare_rejects_missing_usage():
    live_app.app.state.lab = SimpleNamespace(source=Mock(return_value=[]))
    response = TestClient(live_app.app).post('/api/requests/00000000-0000-0000-0000-000000000001/prepare',
                                            json={}, headers={'X-Lab-Action':'1'})
    assert response.status_code == 409
