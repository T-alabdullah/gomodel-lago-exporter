"""Save and load the exporter's notes (its "memory") in its own Postgres.

Every method runs in its own transaction. To make several calls succeed or
fail together (e.g. "record these rows AND move the cursor"), wrap them:

    with store.transaction():
        store.record_row(...)
        store.advance_cursor(...)
"""

import json
import dataclasses
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import psycopg

from exporter.contracts import MappingOutcome, MappingResult, RowStatus, SendResult, TokenKind, UsageRow
from exporter.delivery import Delivery

SCHEMA_FILE = Path(__file__).with_name("schema.sql")
CURSOR_NAME = "usage"


class ExporterBusyError(RuntimeError):
    """Another runner/backfill owns the writer lock in this state database."""


@dataclass(frozen=True, order=True)
class Cursor:
    """Position in GoModel's usage table: the last (timestamp, id) handled.

    order=True lets cursors be compared the same way Postgres sorts rows:
    by timestamp first, then by id.
    """

    timestamp: datetime
    row_id: str


class StateStore:
    def __init__(self, dsn: str) -> None:
        # autocommit=True: each statement commits on its own unless it runs
        # inside `with self.transaction():`.
        self._conn = psycopg.connect(dsn, autocommit=True)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "StateStore":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """All-or-nothing block. Nested blocks become savepoints."""
        with self._conn.transaction():
            yield

    @contextmanager
    def writer_lock(self) -> Iterator[None]:
        # A session lock avoids holding an SQL transaction open during HTTP.
        # Every exporter writer (run, backfill, send-row) uses the same lock.
        lock_id = 70671420261006
        if not self._conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,)).fetchone()[0]:
            raise ExporterBusyError("Another exporter writer is active; retry after it finishes.")
        try:
            yield
        finally:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))

    # --- Delivery intent --------------------------------------------------

    def get_delivery(self, row_id: str) -> Delivery | None:
        found = self._conn.execute(
            "SELECT payload FROM deliveries WHERE usage_row_id = %s", (row_id,),
        ).fetchone()
        return Delivery.from_dict(found[0]) if found else None

    def prepare_delivery(self, delivery: Delivery) -> Delivery:
        """Commit the first payload; a retry can only change its pending flag."""
        saved = self._conn.execute(
            """INSERT INTO deliveries (usage_row_id, payload) VALUES (%s, %s)
               ON CONFLICT (usage_row_id) DO UPDATE
                   SET pending = TRUE, updated_at = now()
               RETURNING payload""",
            (delivery.row.id, json.dumps(delivery.to_dict(), default=str)),
        ).fetchone()
        return Delivery.from_dict(saved[0])

    def pending_deliveries(self, limit: int, after_id: str | None = None) -> list[Delivery]:
        rows = self._conn.execute(
            """SELECT payload FROM deliveries WHERE pending
                   AND (%s::uuid IS NULL OR usage_row_id > %s::uuid)
               ORDER BY usage_row_id LIMIT %s""", (after_id, after_id, limit),
        ).fetchall()
        return [Delivery.from_dict(row[0]) for row in rows]

    def finish_delivery(self, row_id: str, results: list[SendResult], pending: bool) -> None:
        acknowledged = {r.event.kind.value: r.event.tokens for r in results if r.counts_as_sent}
        errors = list(dict.fromkeys(r.error for r in results if r.error))
        self._conn.execute(
            """UPDATE deliveries SET pending = %s,
                   acknowledged = acknowledged || %s::jsonb,
                   last_error = %s, updated_at = now() WHERE usage_row_id = %s""",
            (pending, json.dumps(acknowledged), "; ".join(errors) or None, row_id),
        )

    def acknowledged_tokens(self, row_id: str) -> dict[TokenKind, int]:
        row = self._conn.execute(
            "SELECT acknowledged FROM deliveries WHERE usage_row_id = %s", (row_id,),
        ).fetchone()
        return {TokenKind(k): v for k, v in row[0].items()} if row else {}

    def legacy_mapping(self, mapping: MappingResult) -> MappingResult:
        """Preserve destinations and recorded dimensions from pre-outbox state."""
        old = self._conn.execute(
            """SELECT external_subscription_id, usage_timestamp, model, provider_name
               FROM usage_rows WHERE usage_row_id = %s""", (mapping.row.id,),
        ).fetchone()
        if old and old[0]:
            row = dataclasses.replace(mapping.row, timestamp=old[1], model=old[2], provider_name=old[3])
            return MappingResult(row, MappingOutcome.MAPPED, old[0], "previously recorded destination")
        return mapping

    def legacy_events(self, row_id: str, events: list) -> list:
        """Sent legacy rows replay their accepted quantities, not edited source counts.

        Before deliveries existed, metric codes and full payloads were not saved.
        Operators must retain the old metric configuration during migration.
        """
        from exporter.events import FIELD_NAMES
        from exporter.contracts import LagoEvent

        old = self._conn.execute(
            """SELECT status, input_tokens_sent, cached_input_tokens_sent, output_tokens_sent
               FROM usage_rows WHERE usage_row_id = %s""", (row_id,),
        ).fetchone()
        if not old:
            return [event for event in events if event.tokens > 0]
        saved = dict(zip(TokenKind, old[1:]))
        # The caller supplies all three kinds, including zero, for legacy recovery.
        recovered: list[LagoEvent] = []
        for event in events:
            count = saved[event.kind] if old[0] == RowStatus.SENT.value else (saved[event.kind] or event.tokens)
            if count > 0:
                recovered.append(dataclasses.replace(
                    event, tokens=count, properties={**event.properties, FIELD_NAMES[event.kind]: count},
                ))
        return recovered

    # --- Schema ------------------------------------------------------------

    def init_schema(self) -> None:
        """Create the tables if they don't exist. Safe to run any number of times."""
        with self.transaction():
            self._conn.execute(SCHEMA_FILE.read_text())

    # --- Cursor ------------------------------------------------------------

    def get_cursor(self) -> Cursor | None:
        """Where reading should continue from, or None on the very first run."""
        row = self._conn.execute(
            "SELECT last_timestamp, last_id FROM export_cursor WHERE name = %s",
            (CURSOR_NAME,),
        ).fetchone()
        return Cursor(row[0], str(row[1])) if row else None

    def advance_cursor(self, cursor: Cursor) -> bool:
        """Move the cursor forward to `cursor`. Never moves it backwards.

        Returns True if it moved, False if `cursor` was not ahead of the
        stored one (e.g. a late batch finishing after a newer one).
        """
        result = self._conn.execute(
            """
            INSERT INTO export_cursor (name, last_timestamp, last_id)
            VALUES (%(name)s, %(ts)s, %(id)s)
            ON CONFLICT (name) DO UPDATE
               SET last_timestamp = EXCLUDED.last_timestamp,
                   last_id        = EXCLUDED.last_id,
                   updated_at     = now()
             WHERE (export_cursor.last_timestamp, export_cursor.last_id)
                 < (EXCLUDED.last_timestamp, EXCLUDED.last_id)
            """,
            {"name": CURSOR_NAME, "ts": cursor.timestamp, "id": cursor.row_id},
        )
        return result.rowcount == 1

    # --- Usage rows --------------------------------------------------------

    def record_row(
        self,
        row: UsageRow,
        status: RowStatus,
        subscription_id: str | None = None,
        tokens_sent: dict[TokenKind, int] | None = None,
        error: str | None = None,
    ) -> None:
        """Save (or update) what happened to one usage row.

        Recording the same row again updates it and counts another attempt;
        it never creates a second line.
        """
        tokens_sent = tokens_sent or {}
        self._conn.execute(
            """
            INSERT INTO usage_rows (
                usage_row_id, usage_timestamp, status, external_subscription_id,
                model, provider_name,
                input_tokens_sent, cached_input_tokens_sent, output_tokens_sent,
                last_error
            ) VALUES (
                %(id)s, %(ts)s, %(status)s, %(sub)s,
                %(model)s, %(provider)s,
                %(in)s, %(cached)s, %(out)s,
                %(error)s
            )
            ON CONFLICT (usage_row_id) DO UPDATE SET
                status                   = EXCLUDED.status,
                external_subscription_id = EXCLUDED.external_subscription_id,
                input_tokens_sent        = EXCLUDED.input_tokens_sent,
                cached_input_tokens_sent = EXCLUDED.cached_input_tokens_sent,
                output_tokens_sent       = EXCLUDED.output_tokens_sent,
                last_error               = EXCLUDED.last_error,
                attempts                 = usage_rows.attempts + 1,
                updated_at               = now()
            """,
            {
                "id": row.id,
                "ts": row.timestamp,
                "status": status.value,
                "sub": subscription_id,
                "model": row.model,
                "provider": row.provider_name,
                "in": tokens_sent.get(TokenKind.INPUT, 0),
                "cached": tokens_sent.get(TokenKind.CACHED, 0),
                "out": tokens_sent.get(TokenKind.OUTPUT, 0),
                "error": error,
            },
        )

    def statuses(self, row_ids: Iterable[str]) -> dict[str, RowStatus]:
        """Status of each row already handled; rows never seen are left out.

        The runner uses this to skip rows it already sent when it re-reads
        the overlap window.
        """
        ids = list(row_ids)
        if not ids:
            return {}
        rows = self._conn.execute(
            "SELECT usage_row_id, status FROM usage_rows WHERE usage_row_id = ANY(%s::uuid[])",
            (ids,),
        ).fetchall()
        return {str(row_id): RowStatus(status) for row_id, status in rows}

    # --- Dead letters ------------------------------------------------------

    def add_dead_letter(
        self,
        row_id: str,
        reason: str,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Put a row in the problem box. If it's already there (open), refresh it."""
        self._conn.execute(
            """
            INSERT INTO dead_letters (usage_row_id, reason, error, payload)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (usage_row_id) WHERE resolved_at IS NULL DO UPDATE SET
                reason  = EXCLUDED.reason,
                error   = EXCLUDED.error,
                payload = EXCLUDED.payload
            """,
            (row_id, reason, error, json.dumps(payload, default=str) if payload else None),
        )

    def resolve_dead_letter(self, row_id: str) -> None:
        """Close the open dead letter for a row (it has now been billed)."""
        self._conn.execute(
            "UPDATE dead_letters SET resolved_at = now() "
            "WHERE usage_row_id = %s AND resolved_at IS NULL",
            (row_id,),
        )

    def open_dead_letters(self, limit: int = 100) -> list[dict[str, Any]]:
        """Newest unresolved dead letters, for the status page (Step 11)."""
        cur = self._conn.execute(
            """
            SELECT usage_row_id, reason, error, created_at
              FROM dead_letters
             WHERE resolved_at IS NULL
             ORDER BY created_at DESC
             LIMIT %s
            """,
            (limit,),
        )
        return [
            {"usage_row_id": str(r[0]), "reason": r[1], "error": r[2], "created_at": r[3]}
            for r in cur.fetchall()
        ]

    # --- Summary -----------------------------------------------------------

    def status_counts(self) -> dict[str, int]:
        """How many rows per status, plus open dead letters (Step 11 uses this)."""
        counts = {status.value: 0 for status in RowStatus}
        for status, n in self._conn.execute(
            "SELECT status, count(*) FROM usage_rows GROUP BY status"
        ).fetchall():
            counts[status] = n
        counts["dead_letters_open"] = self._conn.execute(
            "SELECT count(*) FROM dead_letters WHERE resolved_at IS NULL"
        ).fetchone()[0]
        return counts
