"""Read-only Lago v1.53.0 API adapter with bounded, checked pagination."""

import json
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

import httpx


class EvidenceUnavailable(RuntimeError):
    pass


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp has no timezone')
    return parsed


def units(value) -> int:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number != number.to_integral_value():
            raise ValueError('invalid token units')
        return int(number)
    except (InvalidOperation, TypeError) as error:
        raise ValueError('invalid token units') from error


def period_bounds(period):
    # Lago serializes an inclusive last second (e.g. 23:59:59). Convert to
    # our half-open range without dropping sub-second events in that second.
    start = instant(period['from_datetime'])
    end = instant(period['to_datetime']).replace(microsecond=0) + timedelta(seconds=1)
    if start >= end:
        raise ValueError('invalid billing period')
    return start, end


class LagoReadAPI:
    def __init__(self, client, settings):
        self.client = client
        self.settings = settings

    def get(self, path, params=None, *, missing_ok=False):
        try:
            response = self.client._http.get(path, params=params)
            if response.status_code == 404 and missing_ok:
                return None
            if response.status_code != 200:
                raise EvidenceUnavailable(f'Lago read HTTP {response.status_code}')
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError('object required')
            return data
        except (httpx.TransportError, ValueError) as error:
            raise EvidenceUnavailable('Lago read unavailable or malformed') from error

    def pages(self, path, key, params):
        page = 1
        total = None
        count = 0
        for _ in range(self.settings.reconciliation_max_pages):
            body = self.get(path, {**params, 'page': page, 'per_page': self.settings.reconciliation_page_size})
            try:
                rows, meta = body[key], body['meta']
                if not isinstance(rows, list) or int(meta['current_page']) != page:
                    raise ValueError('invalid page')
                observed_total = int(meta['total_count'])
                if total is not None and total != observed_total:
                    raise ValueError('collection changed while paginating')
                total = observed_total
                count += len(rows)
                next_page = meta.get('next_page')
                if next_page is not None and (int(next_page) != page + 1 or not rows):
                    raise ValueError('non-progressing pagination')
                if next_page is None and count != total:
                    raise ValueError('truncated collection')
            except (KeyError, TypeError, ValueError) as error:
                raise EvidenceUnavailable('Lago pagination incomplete or unstable') from error
            yield from rows
            if next_page is None:
                return
            page = int(next_page)
        raise EvidenceUnavailable('Lago pagination limit reached')

    def events(self, start, end, subscription=None):
        if start.microsecond % 1000 or end.microsecond % 1000:
            raise EvidenceUnavailable('Lago serializes event times to milliseconds; use millisecond-aligned bounds')
        params = {'timestamp_from': start.isoformat(), 'timestamp_to': end.isoformat()}
        if subscription:
            params['external_subscription_id'] = subscription
        seen = set()
        events = []
        for event in self.pages('/api/v1/events', 'events', params):
            try:
                key = (event['external_subscription_id'], event['transaction_id'])
                timestamp = instant(event['timestamp'])
                if key in seen:
                    raise ValueError('event repeated between pages')
                seen.add(key)
                if start.replace(microsecond=(start.microsecond // 1000)*1000) <= timestamp < end:
                    events.append(event)
            except (KeyError, TypeError, ValueError) as error:
                raise EvidenceUnavailable('Lago events malformed or unstable') from error
        return events

    def subscription(self, external_id):
        path = '/api/v1/subscriptions/' + quote(external_id, safe='')
        body = self.get(path, missing_ok=True)
        if body is None:
            body = self.get(path, {'status': 'terminated'}, missing_ok=True)
        if body is None:
            raise EvidenceUnavailable(f'Subscription unavailable: {external_id}')
        try:
            sub = body['subscription']
            if not sub['external_customer_id'] or sub['external_id'] != external_id:
                raise ValueError('subscription identity mismatch')
            return sub
        except (KeyError, TypeError, ValueError) as error:
            raise EvidenceUnavailable('Subscription identity unavailable') from error

    def billing_periods(self, sub, start, end):
        customer = quote(sub['external_customer_id'], safe='')
        params = {'external_subscription_id': sub['external_id'], 'apply_taxes': 'false'}
        periods = []
        if sub.get('status') == 'active':
            body = self.get(f'/api/v1/customers/{customer}/current_usage', params)
            period = body.get('customer_usage')
            if not isinstance(period, dict):
                raise EvidenceUnavailable('Current usage missing')
            lo, hi = period_bounds(period)
            if lo < end and hi > start:
                periods.append(('current', period))
            if lo <= start:
                return periods
        for period in self.pages(f'/api/v1/customers/{customer}/past_usage', 'usage_periods', params):
            lo, hi = period_bounds(period)
            if lo < end and hi > start:
                periods.append(('past', period))
        return periods

    def current_model_usage(self, sub, model):
        # Lago's public API has no arbitrary-date or force-refresh option.
        # A nonempty group filter disables ChargeCacheMiddleware in v1.53.0.
        customer = quote(sub['external_customer_id'], safe='')
        body = self.get(f'/api/v1/customers/{customer}/current_usage', {
            'external_subscription_id': sub['external_id'], 'apply_taxes': 'false',
            'filter_by_group': json.dumps({'model': [model]}),
        })
        return body['customer_usage']
