"""Thin HTTP client for the parts of Lago's API the exporter uses.

It only sends requests and reads answers. Deciding what an answer *means*
(accepted, duplicate, rejected, retry) is in `classify` below, and what to
*do* about it is the sender's job.

Lago behaviour this relies on (checked in lago-api for Lago v1.53.0):
  - POST /api/v1/events/batch takes up to 100 events and stores all or none.
  - A transaction_id already stored for the same subscription is answered with
    422 and error_details ... "transaction_id": ["value_already_exist"].
    In a batch, the details are keyed by the event's position ("0", "1", ...).
  - Events for an unknown subscription are NOT rejected at ingestion: Lago
    stores them and silently never bills them. So we check subscriptions
    ourselves before sending (see subscription_is_active).
"""

from typing import Any

import httpx

from exporter.contracts import LagoEvent, SendOutcome


class LagoAuthError(Exception):
    """Lago rejected the API key. Nothing can be sent until config is fixed."""


class LagoClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,   # tests pass a fake Lago here
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def post_batch(self, events: list[LagoEvent]) -> httpx.Response:
        return self._checked(self._http.post(
            "/api/v1/events/batch", json={"events": [e.to_payload() for e in events]},
        ))

    def post_one(self, event: LagoEvent) -> httpx.Response:
        return self._checked(self._http.post("/api/v1/events", json={"event": event.to_payload()}))

    def get_subscription(self, external_id: str) -> httpx.Response:
        """200 if an ACTIVE subscription has this id, 404 otherwise."""
        return self._checked(self._http.get(f"/api/v1/subscriptions/{external_id}"))

    @staticmethod
    def _checked(response: httpx.Response) -> httpx.Response:
        if response.status_code in (401, 403):
            raise LagoAuthError(
                f"Lago rejected the API key (HTTP {response.status_code}). "
                "Check EXPORTER_LAGO_API_KEY."
            )
        return response


# --- Understanding Lago's answers -----------------------------------------

def is_retryable(status_code: int) -> bool:
    """Temporary problems worth retrying: rate limited, or a server error."""
    return status_code == 429 or status_code >= 500


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
        return body if isinstance(body, dict) else {}
    except ValueError:
        return {}


def is_duplicate(details: Any) -> bool:
    """True if Lago's error details say only 'this transaction_id already exists'."""
    return (
        isinstance(details, dict)
        and set(details) == {"transaction_id"}
        and "value_already_exist" in (details.get("transaction_id") or [])
    )


def classify_single(response: httpx.Response) -> tuple[SendOutcome, str | None]:
    """Outcome of sending ONE event (retryable statuses are handled before this)."""
    if response.is_success:
        return SendOutcome.ACCEPTED, None
    body = _json(response)
    if response.status_code == 422 and is_duplicate(body.get("error_details")):
        return SendOutcome.DUPLICATE, None
    return SendOutcome.REJECTED, f"HTTP {response.status_code}: {response.text[:500]}"