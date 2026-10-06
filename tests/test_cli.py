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
