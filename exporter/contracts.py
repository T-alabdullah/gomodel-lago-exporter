"""The data that flows between the exporter's stages.

    reader  ->  UsageRow
    mapper  ->  MappingResult
    events  ->  LagoEvent (0-3 per row)
    sender  ->  SendResult (one per event)

Every module talks to the next one only through these types, so each stage
can be built, tested and explained on its own.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class TokenKind(StrEnum):
    """The three billable token types. The value is the transaction_id suffix."""

    INPUT = "in"         # uncached input tokens
    CACHED = "cached"    # cached input tokens
    OUTPUT = "out"       # output tokens


class MappingOutcome(StrEnum):
    MAPPED = "mapped"              # billable and matched to a Lago subscription
    NOT_BILLABLE = "not_billable"  # provider not in billable_providers (or a cache hit we don't bill)
    UNMAPPED = "unmapped"          # billable but no subscription found -> dead letter + alert


class RowStatus(StrEnum):
    """Final status of a usage row, stored in the exporter's state DB."""

    SENT = "sent"
    FAILED = "failed"
    UNMAPPED = "unmapped"
    NOT_BILLABLE = "not_billable"


class SendOutcome(StrEnum):
    ACCEPTED = "accepted"    # Lago stored it
    DUPLICATE = "duplicate"  # Lago already had it (422 value_already_exist) -> counts as sent
    REJECTED = "rejected"    # permanent error -> dead letter
    RETRY = "retry"          # temporary error (429/5xx/network) and retries ran out


@dataclass(frozen=True)
class UsageRow:
    """One row of GoModel's `usage` table (only the columns we use)."""

    id: str                      # UUID, unique per row -> base of transaction_id
    request_id: str              # NOT unique: one request can have several rows
    timestamp: datetime          # timezone-aware
    model: str
    provider: str                # provider type, e.g. "ollama"
    provider_name: str | None    # configured name, e.g. "ollama-qai"
    endpoint: str
    user_path: str | None
    labels: tuple[str, ...]
    cache_type: str | None       # "exact", "semantic" or None
    input_tokens: int
    output_tokens: int
    total_tokens: int
    raw_data: dict[str, Any] = field(default_factory=dict)

    @property
    def is_cache_hit(self) -> bool:
        return bool(self.cache_type)


@dataclass(frozen=True)
class MappingResult:
    row: UsageRow
    outcome: MappingOutcome
    external_subscription_id: str | None = None
    reason: str = ""             # human-readable, shown in dead letter and status page


def transaction_id(usage_row_id: str, kind: TokenKind) -> str:
    """The idempotency key Lago deduplicates on. Never change this format."""
    return f"{usage_row_id}:{kind.value}"


@dataclass(frozen=True)
class LagoEvent:
    usage_row_id: str
    kind: TokenKind
    code: str                    # billable metric code
    external_subscription_id: str
    timestamp: datetime
    tokens: int                  # also stored in properties under the metric's field_name
    properties: dict[str, Any]

    @property
    def transaction_id(self) -> str:
        return transaction_id(self.usage_row_id, self.kind)

    def to_payload(self) -> dict[str, Any]:
        """The event exactly as Lago's API expects it (inside "event" / "events")."""
        return {
            "transaction_id": self.transaction_id,
            "external_subscription_id": self.external_subscription_id,
            "code": self.code,
            "timestamp": self.timestamp.timestamp(),  # unix seconds, with decimals
            "properties": self.properties,
        }


@dataclass(frozen=True)
class SendResult:
    event: LagoEvent
    outcome: SendOutcome
    error: str | None = None

    @property
    def counts_as_sent(self) -> bool:
        return self.outcome in (SendOutcome.ACCEPTED, SendOutcome.DUPLICATE)