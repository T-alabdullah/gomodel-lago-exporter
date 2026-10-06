"""Deliver events to Lago without losing or double-billing anything.

    1. Check each subscription is active in Lago, and that the usage happened
       after it started. Lago would otherwise accept such events and silently
       never bill them.
    2. Send in batches of up to 100.
    3. Batch refused (422)? Lago stored none of it. Resend one event at a time:
       new events get stored, duplicates are recognised, the bad one is found.
    4. Rate limited / server error / network down? Wait and retry, with
       exponential backoff and jitter. If retries run out, report RETRY:
       nothing is lost, the runner tries again next cycle.

Every event gets exactly one SendResult (see contracts.SendOutcome).
"""

import logging
import random
import time
from collections.abc import Callable
from datetime import datetime

import httpx

from exporter.config import Settings
from exporter.contracts import LagoEvent, SendOutcome, SendResult
from exporter.lago_client import LagoClient, classify_single, is_retryable

log = logging.getLogger(__name__)

SUBSCRIPTION_CACHE_SECONDS = 300


class RetriesExhausted(Exception):
    """A temporary problem outlasted every retry."""


class Sender:
    def __init__(
        self,
        client: LagoClient,
        settings: Settings,
        sleep: Callable[[float], None] = time.sleep,     # tests pass a fake clock
        rand: Callable[[], float] = random.random,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sleep = sleep
        self._rand = rand
        self._now = now
        # id -> (when the subscription started, when we last checked it)
        self._active_subscriptions: dict[str, tuple[datetime, float]] = {}

    # --- Public -----------------------------------------------------------

    def send(self, events: list[LagoEvent]) -> list[SendResult]:
        results: list[SendResult] = []
        sendable: list[LagoEvent] = []

        for sub_id in dict.fromkeys(e.external_subscription_id for e in events):
            subs_events = [e for e in events if e.external_subscription_id == sub_id]
            try:
                started_at = self._subscription_start(sub_id)
            except RetriesExhausted as err:
                results += [SendResult(e, SendOutcome.RETRY, f"subscription check: {err}")
                            for e in subs_events]
                continue
            for e in subs_events:
                if started_at is None:
                    results.append(SendResult(e, SendOutcome.REJECTED,
                                              f"unknown or inactive Lago subscription {sub_id!r}"))
                elif e.timestamp < started_at:
                    results.append(SendResult(e, SendOutcome.REJECTED,
                                              f"usage at {e.timestamp.isoformat()} is before "
                                              f"subscription {sub_id!r} started at {started_at.isoformat()}"))
                else:
                    sendable.append(e)

        size = self._settings.lago_batch_size
        for start in range(0, len(sendable), size):
            results += self._send_batch(sendable[start:start + size])
        return results

    # --- Steps ------------------------------------------------------------

    def _subscription_start(self, sub_id: str) -> datetime | None:
        """When the active subscription started, or None if there is none."""
        cached = self._active_subscriptions.get(sub_id)
        if cached is not None and self._now() - cached[1] < SUBSCRIPTION_CACHE_SECONDS:
            return cached[0]
        response = self._with_retries(lambda: self._client.get_subscription(sub_id))
        if response.status_code == 404:
            return None    # not cached: a newly created subscription is picked up next time
        if response.status_code != 200:
            raise RetriesExhausted(f"unexpected subscription response HTTP {response.status_code}")
        try:
            started_at = datetime.fromisoformat(response.json()["subscription"]["started_at"])
            if started_at.tzinfo is None:
                raise ValueError("started_at has no timezone")
        except (ValueError, TypeError, KeyError) as err:
            raise RetriesExhausted("invalid subscription response; cannot verify billing period") from err
        self._active_subscriptions[sub_id] = (started_at, self._now())
        return started_at

    def _send_batch(self, batch: list[LagoEvent]) -> list[SendResult]:
        try:
            response = self._with_retries(lambda: self._client.post_batch(batch))
        except RetriesExhausted as err:
            return [SendResult(e, SendOutcome.RETRY, str(err)) for e in batch]

        if response.is_success:
            return [SendResult(e, SendOutcome.ACCEPTED) for e in batch]

        # Lago stored nothing from this batch. Find out event by event.
        log.info("batch of %d refused (HTTP %d); resending one at a time",
                 len(batch), response.status_code)
        return [self._send_one(e) for e in batch]

    def _send_one(self, event: LagoEvent) -> SendResult:
        try:
            response = self._with_retries(lambda: self._client.post_one(event))
        except RetriesExhausted as err:
            return SendResult(event, SendOutcome.RETRY, str(err))
        outcome, error = classify_single(response)
        return SendResult(event, outcome, error)

    def _with_retries(self, call: Callable[[], httpx.Response]) -> httpx.Response:
        """Call, retrying temporary failures with exponential backoff and jitter."""
        attempts = self._settings.max_retries + 1
        for attempt in range(attempts):
            try:
                response = call()
                if not is_retryable(response.status_code):
                    return response
                problem = f"HTTP {response.status_code}"
            except httpx.TransportError as err:          # connection refused, timeout, ...
                problem = f"{err.__class__.__name__}: {err}"

            if attempt == attempts - 1:
                raise RetriesExhausted(f"{problem} (gave up after {attempts} attempts)")
            delay = self.backoff_delay(attempt)
            log.warning("Lago temporary failure (%s); retry %d/%d in %.1fs",
                        problem, attempt + 1, attempts - 1, delay)
            self._sleep(delay)
        raise AssertionError("unreachable")

    def backoff_delay(self, attempt: int) -> float:
        """0.5s, 1s, 2s, 4s ... capped, then randomised to between 50% and 100%.

        The randomness ("jitter") stops many senders retrying in lockstep.
        """
        ceiling = min(self._settings.backoff_max_seconds,
                      self._settings.backoff_base_seconds * 2 ** attempt)
        return ceiling * (0.5 + 0.5 * self._rand())
