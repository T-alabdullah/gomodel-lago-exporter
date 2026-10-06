"""Executable provider and provisioning contracts used by the Compose demo."""
import json
import os
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from demo.mock_provider import app, MODELS
from demo.traffic import request
from scripts import gomodel_setup, lago_setup


@pytest.mark.parametrize('model', MODELS)
@pytest.mark.parametrize('stream', [False, True])
def test_mock_stream_and_plain_have_same_exact_usage(model, stream):
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={'model': model, 'stream': stream})
        assert response.status_code == 200
        if stream:
            lines = response.text.splitlines()
            assert lines[-2] == 'data: [DONE]'
            chunks = [json.loads(line[6:]) for line in lines if line.startswith('data: {')]
            assert chunks[0]['choices'][0]['delta']['content'] == 'Hello.'
            usage = chunks[-1]['usage']
        else:
            usage = response.json()['usage']
        prompt, cached, output = MODELS[model]
        assert usage == {'prompt_tokens': prompt, 'completion_tokens': output,
                         'total_tokens': prompt+output, 'prompt_tokens_details': {'cached_tokens': cached}}


def test_mock_discovery_and_unknown_model():
    with TestClient(app) as client:
        assert {m['id'] for m in client.get('/v1/models').json()['data']} == set(MODELS)
        assert client.post('/api/show', json={'model': 'demo-small'}).json()['capabilities'] == ['completion']
        assert client.post('/v1/chat/completions', json={'model': 'unknown'}).status_code == 404


def test_traffic_rejects_truncated_stream():
    with httpx.Client(base_url='http://test', transport=httpx.MockTransport(lambda r:
            httpx.Response(200, text='data: {"usage": {"prompt_tokens":120,"completion_tokens":12}}\n\n'))) as client:
        with pytest.raises(AssertionError, match='finish'):
            request(client, 'secret', 'demo-small', 'ollama-qai', True)


def test_demo_secrets_are_private_and_rerun_preserves_them(tmp_path):
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'scripts/init_demo.py'
    subprocess.run([sys.executable, str(script)], cwd=tmp_path, check=True, capture_output=True)
    path = tmp_path / '.env.demo'
    original = path.read_bytes()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b'LAGO_RSA_PRIVATE_KEY=' in original
    subprocess.run([sys.executable, str(script)], cwd=tmp_path, check=True, capture_output=True)
    assert path.read_bytes() == original


def test_missing_saved_gomodel_secret_is_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(gomodel_setup, 'KEYS_FILE', tmp_path/'keys.json')
    def fake(req):
        assert req.method == 'GET'
        if req.url.path == '/health':
            return httpx.Response(200)
        return httpx.Response(200, json=[{'name': 'acme', 'active': True}])
    client = httpx.Client(base_url='http://test', transport=httpx.MockTransport(fake))
    monkeypatch.setattr(gomodel_setup.httpx, 'Client', lambda **kwargs: client)
    with pytest.raises(RuntimeError, match='no saved secret'):
        gomodel_setup.main()


def test_gomodel_setup_persists_each_secret_before_next_create(tmp_path, monkeypatch):
    path = tmp_path/'keys.json'
    monkeypatch.setattr(gomodel_setup, 'KEYS_FILE', path)
    created = []
    def fake(req):
        if req.url.path == '/health':
            return httpx.Response(200)
        if req.method == 'GET':
            return httpx.Response(200, json=[])
        name = json.loads(req.content)['name']
        if created:
            assert json.loads(path.read_text())[created[-1]] == 'secret-'+created[-1]
        created.append(name)
        return httpx.Response(200, json={'value': 'secret-'+name})
    client = httpx.Client(base_url='http://test', transport=httpx.MockTransport(fake))
    monkeypatch.setattr(gomodel_setup.httpx, 'Client', lambda **kwargs: client)
    gomodel_setup.main()
    assert len(json.loads(path.read_text())) == 3
    assert path.stat().st_mode & 0o777 == 0o600
