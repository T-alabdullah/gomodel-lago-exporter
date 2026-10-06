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
