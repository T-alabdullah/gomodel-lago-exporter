import json
from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

from exporter.cli import app, _parse_boundary
from exporter.config import get_settings


def test_dates_are_midnight_utc():
    assert _parse_boundary('2026-10-05') == datetime(2026, 10, 5, tzinfo=timezone.utc)
    assert _parse_boundary('2026-10-05T03:00:00+03:00') == _parse_boundary('2026-10-05')


@pytest.mark.parametrize('start,end', [
    ('not-a-date', '2026-10-06'), ('2026-10-06', '2026-10-05'),
    ('2026-10-05', '2026-10-05'), ('2026-10-05T00:00:00', '2026-10-06'),
])
def test_invalid_backfill_cli_fails_before_database_access(start, end):
    result = CliRunner().invoke(app, ['backfill', start, end])
    assert result.exit_code == 2


def test_config_masks_database_passwords_and_api_key(monkeypatch):
    monkeypatch.setenv('EXPORTER_GOMODEL_DB_URL', 'postgresql://user:private-password@localhost/db')
    monkeypatch.setenv('EXPORTER_STATE_DB_URL', "host=localhost dbname=db password='state-private'")
    monkeypatch.setenv('EXPORTER_LAGO_API_KEY', 'private-api-key')
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ['config'])
        assert result.exit_code == 0
        assert all(secret not in result.output for secret in ['private-password', 'state-private', 'private-api-key'])
        assert json.loads(result.output)['lago_api_key'] == '**********'
    finally:
        get_settings.cache_clear()


def test_successful_backfill_cli_passes_exact_boundaries(monkeypatch):
    import exporter.cli as cli
    calls = []
    class Runner:
        def backfill(self, start, end):
            calls.append((start, end))
    monkeypatch.setattr(cli, '_replay', lambda action: action(Runner()))
    result = CliRunner().invoke(app, ['backfill', '2026-10-05', '2026-10-06'])
    assert result.exit_code == 0
    assert calls == [(_parse_boundary('2026-10-05'), _parse_boundary('2026-10-06'))]


@pytest.mark.parametrize('args', [
    ['--start', '2026-10-05'], ['--end', '2026-10-06'],
    ['--start', '2026-10-06', '--end', '2026-10-05'],
    ['--start', '2026-10-05T00:00:00', '--end', '2026-10-06'],
])
def test_invalid_reconcile_range_fails_before_database_access(args):
    result = CliRunner().invoke(app, ['reconcile', *args])
    assert result.exit_code == 2


@pytest.mark.parametrize('status,exit_code', [('matched', 0), ('mismatch', 1), ('incomplete', 1)])
def test_reconcile_exit_codes(monkeypatch, status, exit_code):
    import exporter.cli as cli
    from types import SimpleNamespace
    closed = []
    reconciler = SimpleNamespace(run=lambda start, end: {'status': status})
    client = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(cli, '_components', lambda: (None,None,None,client,reconciler))
    result = CliRunner().invoke(app, ['reconcile', '--start', '2026-10-05', '--end', '2026-10-06'])
    assert result.exit_code == exit_code
    assert closed == [True]
