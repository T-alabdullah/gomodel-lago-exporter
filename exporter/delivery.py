"""Durable, immutable delivery intent saved before the first HTTP request."""

import dataclasses
from datetime import datetime

from exporter.contracts import LagoEvent, TokenKind, UsageRow


@dataclasses.dataclass(frozen=True)
class Delivery:
    row: UsageRow
    subscription_id: str
    events: list[LagoEvent]

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Delivery":
        row = dict(data["row"])
        row["timestamp"] = datetime.fromisoformat(row["timestamp"])
        row["labels"] = tuple(row["labels"])
        events = []
        for raw in data["events"]:
            event = dict(raw)
            event["kind"] = TokenKind(event["kind"])
            event["timestamp"] = datetime.fromisoformat(event["timestamp"])
            events.append(LagoEvent(**event))
        return cls(UsageRow(**row), data["subscription_id"], events)
