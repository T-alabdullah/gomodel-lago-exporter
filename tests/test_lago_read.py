from datetime import datetime, timezone

import pytest

from exporter.lago_read import EvidenceUnavailable, LagoReadAPI, period_bounds, units
from tests.factories import settings
from tests.fake_lago_read import ReadFakeLago


@pytest.mark.parametrize('value', ['NaN', 'Infinity', '-1', '1.5', None, True, 'invalid'])
def test_invalid_units_are_never_silently_rounded(value):
    with pytest.raises(ValueError):
        units(value)


def test_decimal_integer_units_preserve_precision():
    assert units('9007199254740993.0') == 9007199254740993


def test_billing_inclusive_last_second_converts_to_half_open():
    lo, hi = period_bounds({'from_datetime': '2026-10-01T00:00:00Z', 'to_datetime': '2026-10-31T23:59:59Z'})
    assert hi == datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_missing_subscription_cannot_supply_billing_evidence():
    lago = ReadFakeLago()
    with pytest.raises(EvidenceUnavailable):
        LagoReadAPI(lago.client(), settings()).subscription('missing')


@pytest.mark.parametrize('rows,total,current,next_page,valid', [
    ([], 0, 0, None, True), ([], 0, 1, None, True),
    ([{}], 1, 0, None, False), ([], 1, 0, None, False),
    ([], 0, 0, 2, False),
])
def test_real_lago_empty_pagination(rows, total, current, next_page, valid):
    from exporter.config import Settings
    from exporter.lago_client import LagoClient
    from exporter.lago_read import LagoReadAPI, EvidenceUnavailable
    import httpx
    client = LagoClient('http://test', 'key', transport=httpx.MockTransport(lambda req:
        httpx.Response(200, json={'events': rows, 'meta': {'current_page': current,
            'next_page': next_page, 'total_count': total}})))
    api = LagoReadAPI(client, Settings())
    try:
        if valid:
            assert list(api.pages('/api/v1/events', 'events', {})) == []
        else:
            with pytest.raises(EvidenceUnavailable):
                list(api.pages('/api/v1/events', 'events', {}))
    finally:
        client.close()
