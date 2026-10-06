"""Pinned Lago REST response shapes for reconciliation contract/failure tests."""

import json
from copy import deepcopy
from datetime import datetime, timezone

import httpx

from tests.fake_lago import FakeLago

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 1, tzinfo=timezone.utc)
CODES = {'llm_input_tokens': 'input_tokens', 'llm_cached_input_tokens': 'cached_input_tokens',
         'llm_output_tokens': 'output_tokens'}


class ReadFakeLago(FakeLago):
    def __init__(self):
        super().__init__()
        self.billing_delta = 0
        self.cached_delta = 0
        self.read_failure = None
        self.bad_pagination = False
        self.terminated = {}
        self.past_periods = []
        self.current_from = START
        self.current_to = END
        self.missing_metric = False
        self.filter_requests = 0

    def _page(self, rows, request, key):
        page = int(request.url.params.get('page', 1))
        size = int(request.url.params.get('per_page', 100))
        total = len(rows)
        if self.bad_pagination and page > 1:
            total += 1
        return httpx.Response(200, json={key: rows[(page-1)*size:page*size], 'meta': {
            'current_page': page, 'next_page': page+1 if page*size < len(rows) else None,
            'total_count': total, 'total_pages': (len(rows)+size-1)//size,
        }})

    def _handle(self, request):
        path = request.url.path
        if request.method != 'GET':
            return super()._handle(request)
        self.requests.append((request.method, path))
        if self.read_failure:
            return httpx.Response(self.read_failure, json={})
        if path.startswith('/api/v1/subscriptions/'):
            sub = path.rsplit('/', 1)[1]
            terminated = request.url.params.get('status') == 'terminated'
            catalog = self.terminated if terminated else self.subscriptions
            if sub not in catalog:
                return httpx.Response(404, json={})
            return httpx.Response(200, json={'subscription': {
                'external_id': sub, 'external_customer_id': sub.removeprefix('sub_'),
                'started_at': catalog[sub].isoformat(), 'status': 'terminated' if terminated else 'active',
            }})
        if path == '/api/v1/events':
            lo = datetime.fromisoformat(request.url.params['timestamp_from']).timestamp()
            hi = datetime.fromisoformat(request.url.params['timestamp_to']).timestamp()
            sub = request.url.params.get('external_subscription_id')
            events = []
            for raw in self.stored.values():
                if lo <= raw['timestamp'] <= hi and (not sub or raw['external_subscription_id'] == sub):
                    event = deepcopy(raw)
                    event['timestamp'] = datetime.fromtimestamp(raw['timestamp'], timezone.utc).isoformat(timespec='milliseconds')
                    event.update(lago_id=event['transaction_id'], lago_customer_id=None, lago_subscription_id=None)
                    events.append(event)
            events.sort(key=lambda e: (e['timestamp'], e['transaction_id']), reverse=True)
            return self._page(events, request, 'events')
        if path.endswith('/past_usage'):
            return self._page(self.past_periods, request, 'usage_periods')
        if path.endswith('/current_usage'):
            sub = request.url.params['external_subscription_id']
            group = json.loads(request.url.params.get('filter_by_group', '{}'))
            if group:
                self.filter_requests += 1
            models = {e['properties']['model'] for e in self.stored.values()} | {'qwen2.5:0.5b'}
            if group:
                models = set(group['model'])
            charges = []
            for code, field in CODES.items():
                if self.missing_metric and code == 'llm_cached_input_tokens':
                    continue
                filters = []
                for model in sorted(models):
                    count = sum(e['properties'].get(field, 0) for e in self.stored.values()
                        if e['code'] == code and e['external_subscription_id'] == sub
                        and e['properties']['model'] == model
                        and self.current_from.timestamp() <= e['timestamp'] < self.current_to.timestamp())
                    if code == 'llm_input_tokens':
                        count += self.billing_delta + (self.cached_delta if not group else 0)
                    filters.append({'values': {'model': [model]}, 'units': str(count)})
                charges.append({'billable_metric': {'code': code, 'aggregation_type': 'sum_agg'},
                                'units': str(sum(int(f['units']) for f in filters)), 'filters': filters})
            from datetime import timedelta
            return httpx.Response(200, json={'customer_usage': {
                'from_datetime': self.current_from.isoformat(),
                'to_datetime': (self.current_to-timedelta(seconds=1)).isoformat(), 'charges_usage': charges,
            }})
        return httpx.Response(404, json={})
