"""A pretend Lago for tests: speaks the same HTTP as the real one, in memory.

It copies the real behaviour the sender depends on (Lago v1.53.0):
  - transaction_id is unique per subscription -> 422 value_already_exist
  - a batch is all-or-nothing; errors are keyed by position ("0", "1", ...)
  - GET /subscriptions/<id> is 200 (with started_at) if active, 404 otherwise
Tests can also make it fail on purpose (429, 503, network down, bad API key).
"""

import json
from datetime import datetime, timezone

import httpx

from exporter.lago_client import LagoClient

BAD_CODE = "not_a_metric"   # events with this code are "invalid" in the fake
LONG_AGO = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeLago:
    def __init__(self, subscriptions=("sub_acme", "sub_beta")) -> None:
        # subscription id -> when it started
        self.subscriptions = {sub: LONG_AGO for sub in subscriptions}
        self.stored: dict[tuple[str, str], dict] = {}   # (subscription, transaction_id) -> event
        self.requests: list[tuple[str, str]] = []       # (method, path) of every request
        self.fail_next: list = []                       # e.g. [503, 503, "network"] then normal

    def client(self) -> LagoClient:
        return LagoClient("http://lago.test", "test-key", transport=httpx.MockTransport(self._handle))

    # --- HTTP -------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if self.fail_next:
            failure = self.fail_next.pop(0)
            if failure == "network":
                raise httpx.ConnectError("connection refused", request=request)
            return httpx.Response(failure, json={"status": failure})

        path = request.url.path
        if request.method == "GET" and path.startswith("/api/v1/subscriptions/"):
            sub = path.rsplit("/", 1)[1]
            if sub not in self.subscriptions:
                return httpx.Response(404, json={"status": 404, "code": "subscription_not_found"})
            started = self.subscriptions[sub].isoformat(timespec="milliseconds").replace("+00:00", "Z")
            return httpx.Response(200, json={"subscription": {"external_id": sub, "started_at": started}})
        body = json.loads(request.content)
        if path == "/api/v1/events/batch":
            return self._batch(body["events"])
        if path == "/api/v1/events":
            return self._single(body["event"])
        return httpx.Response(404, json={})

    def _problem(self, event: dict, seen: set) -> dict | None:
        key = (event["external_subscription_id"], event["transaction_id"])
        if event["code"] == BAD_CODE:
            return {"code": ["value_is_invalid"]}
        if key in self.stored or key in seen:
            return {"transaction_id": ["value_already_exist"]}
        return None

    def _batch(self, events: list[dict]) -> httpx.Response:
        errors, seen = {}, set()
        for i, event in enumerate(events):
            problem = self._problem(event, seen)
            if problem:
                errors[str(i)] = problem
            seen.add((event["external_subscription_id"], event["transaction_id"]))
        if errors:   # all or nothing
            return httpx.Response(422, json={"status": 422, "code": "validation_errors",
                                             "error_details": errors})
        for event in events:
            self.stored[(event["external_subscription_id"], event["transaction_id"])] = event
        return httpx.Response(200, json={"events": events})

    def _single(self, event: dict) -> httpx.Response:
        problem = self._problem(event, set())
        if problem:
            return httpx.Response(422, json={"status": 422, "code": "validation_errors",
                                             "error_details": problem})
        self.stored[(event["external_subscription_id"], event["transaction_id"])] = event
        return httpx.Response(200, json={"event": event})

    # --- Helpers for assertions -------------------------------------------

    def count(self, method: str, path: str) -> int:
        return sum(1 for r in self.requests if r == (method, path))