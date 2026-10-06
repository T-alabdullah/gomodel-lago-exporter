"""Read usage rows from GoModel's database, oldest first, in batches.

Rows are read in (timestamp, id) order. "Keyset" paging is used: each read
asks for rows *after* a position, never "page 3". New rows arriving while we
read can't shift the pages, so no row is ever skipped or read twice by paging.

The reader never writes: it connects with the read-only `exporter_ro` login.
"""

from datetime import datetime, timedelta

import psycopg

from exporter.contracts import UsageRow
from exporter.state.store import Cursor

# The smallest possible UUID: "the very start of this timestamp".
MIN_ID = "00000000-0000-0000-0000-000000000000"

COLUMNS = """
    id, request_id, timestamp, model, provider, provider_name, endpoint,
    user_path, labels, cache_type, input_tokens, output_tokens, total_tokens,
    raw_data
"""


def overlap_start(cursor: Cursor | None, overlap_seconds: int) -> Cursor | None:
    """Where a cycle should start reading: `overlap_seconds` behind the cursor.

    Re-reading this window catches rows that landed late, behind the cursor
    (buffered writes, several GoModel replicas). Rows already handled are
    skipped later using the state database. None means "from the beginning".
    """
    if cursor is None:
        return None
    return Cursor(cursor.timestamp - timedelta(seconds=overlap_seconds), MIN_ID)


class UsageReader:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn

    def read_after(
        self,
        position: Cursor | None,
        limit: int,
        until: datetime | None = None,
        since: datetime | None = None,
    ) -> list[UsageRow]:
        """Up to `limit` rows strictly after `position`, oldest first.

        position=None starts from the very first row.
        until (optional) stops before that time; backfill uses it for date ranges.
        """
        conditions, params = [], {"limit": limit}
        if position is not None:
            conditions.append("(timestamp, id) > (%(ts)s, %(id)s::uuid)")
            params.update(ts=position.timestamp, id=position.row_id)
        if until is not None:
            conditions.append("timestamp < %(until)s")
            params["until"] = until
        if since is not None:
            conditions.append("timestamp >= %(since)s")
            params["since"] = since
        where = "WHERE " + " AND ".join(conditions) if conditions else ""

        query = f"SELECT {COLUMNS} FROM usage {where} ORDER BY timestamp, id LIMIT %(limit)s"
        # A fresh connection per read: if GoModel's database restarts,
        # the next read simply reconnects.
        with psycopg.connect(self._dsn, autocommit=True, connect_timeout=10) as conn:
            return [_to_usage_row(r) for r in conn.execute(query, params).fetchall()]

    def find_by_id_prefix(self, prefix: str) -> list[UsageRow]:
        """At most two matches, across the entire table; prefix validated by CLI."""
        with psycopg.connect(self._dsn, autocommit=True, connect_timeout=10) as conn:
            rows = conn.execute(
                f"SELECT {COLUMNS} FROM usage WHERE id::text LIKE %s ORDER BY id LIMIT 2",
                (prefix + "%",),
            ).fetchall()
        return [_to_usage_row(row) for row in rows]


def _to_usage_row(r: tuple) -> UsageRow:
    """Turn one database row (in COLUMNS order) into a UsageRow."""
    (row_id, request_id, timestamp, model, provider, provider_name, endpoint,
     user_path, labels, cache_type, input_tokens, output_tokens, total_tokens,
     raw_data) = r
    return UsageRow(
        id=str(row_id),
        request_id=request_id,
        timestamp=timestamp,
        model=model,
        provider=provider,
        provider_name=provider_name,
        endpoint=endpoint,
        user_path=user_path,
        labels=tuple(labels or ()),          # JSONB array, or NULL
        cache_type=cache_type or None,       # "" and NULL both mean "not a cache hit"
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        total_tokens=total_tokens or 0,
        raw_data=raw_data or {},             # JSONB object, or NULL
    )
