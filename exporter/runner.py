"""The export loop: read -> map -> build -> send -> record, every few seconds.

One cycle:
  1. Start `overlap_window_seconds` behind the cursor (catches late rows).
  2. Read rows page by page, oldest first.
  3. Skip rows the state database already knows.
  4. Map each new row, build its events, send them.
  5. In ONE transaction: record every row's outcome, dead-letter the problems,
     and move the cursor forward.

The no-loss rule: a row whose events got RETRY (Lago down, rate limited) is
NOT recorded, and the cursor never moves past it. The cycle stops there and
the next cycle tries again. A crash at any point is safe: whatever was sent but
not recorded is re-sent next time, and Lago answers "duplicate".
"""

import dataclasses
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass

from exporter.config import Settings
from exporter.contracts import (
    MappingOutcome, MappingResult, RowStatus, SendOutcome, SendResult, UsageRow,
)
from exporter.events import build_events
from exporter.lago_client import LagoAuthError
from exporter.mapper import map_row
from exporter.reader import UsageReader, overlap_start
from exporter.sender import Sender
from exporter.state.store import Cursor, StateStore

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    rows_read: int = 0
    rows_skipped: int = 0        # already handled in an earlier cycle
    sent: int = 0
    not_billable: int = 0
    unmapped: int = 0
    failed: int = 0
    waiting_for_retry: int = 0   # Lago trouble: will be tried again next cycle
    events_sent: int = 0
    cursor: Cursor | None = None

    @property
    def did_work(self) -> bool:
        return self.rows_read > self.rows_skipped

    def summary(self) -> str:
        position = self.cursor.timestamp.isoformat() if self.cursor else "start"
        return (f"read {self.rows_read} (skipped {self.rows_skipped} known): "
                f"{self.sent} sent ({self.events_sent} events), {self.not_billable} not billable, "
                f"{self.unmapped} unmapped, {self.failed} failed, "
                f"{self.waiting_for_retry} waiting for retry; cursor at {position}")


class Runner:
    def __init__(
        self,
        settings: Settings,
        open_store: Callable[[], StateStore],   # a fresh connection each cycle
        reader: UsageReader,
        sender: Sender,
    ) -> None:
        self._settings = settings
        self._open_store = open_store
        self._reader = reader
        self._sender = sender

    # --- The loop ---------------------------------------------------------

    def run_forever(self, stop: threading.Event | None = None) -> None:
        stop = stop or threading.Event()
        while not stop.is_set():
            try:
                report = self.run_cycle()
                # Busy cycles at INFO; idle ones (nothing new) only at DEBUG, to keep logs quiet.
                log.log(logging.INFO if report.did_work else logging.DEBUG, report.summary())
            except LagoAuthError:
                raise                       # config problem: retrying won't help
            except Exception:
                log.exception("cycle failed; trying again next cycle")
            stop.wait(self._settings.poll_interval_seconds)

    # --- One cycle --------------------------------------------------------

    def run_cycle(self) -> CycleReport:
        report = CycleReport()
        with self._open_store() as store:
            report.cursor = store.get_cursor()
            position = overlap_start(report.cursor, self._settings.overlap_window_seconds)
            while rows := self._reader.read_after(position, self._settings.read_batch_size):
                must_stop = self._process_page(store, rows, report)
                if must_stop:
                    break                   # Lago trouble: try again next cycle
                position = Cursor(rows[-1].timestamp, rows[-1].id)
            report.cursor = store.get_cursor()
        return report

    def _process_page(self, store: StateStore, rows: list[UsageRow], report: CycleReport) -> bool:
        """Handle one page of rows. Returns True if the cycle must stop (retry later)."""
        report.rows_read += len(rows)
        known = store.statuses(r.id for r in rows)
        report.rows_skipped += len(known)
        new_rows = [r for r in rows if r.id not in known]

        mappings = {r.id: map_row(r, self._settings) for r in new_rows}
        events = [e for m in mappings.values() if m.outcome is MappingOutcome.MAPPED
                  for e in build_events(m, self._settings)]
        results: dict[str, list[SendResult]] = defaultdict(list)
        for result in (self._sender.send(events) if events else []):
            results[result.event.usage_row_id].append(result)

        cursor_target: Cursor | None = None
        must_stop = False
        with store.transaction():
            for row in rows:
                if row.id in known:
                    pass                    # handled before
                elif self._needs_retry(results[row.id]):
                    report.waiting_for_retry += 1
                    must_stop = True        # leave it unrecorded; don't move past it
                else:
                    self._record(store, mappings[row.id], results[row.id], report)
                if not must_stop:
                    cursor_target = Cursor(row.timestamp, row.id)
            if cursor_target:
                store.advance_cursor(cursor_target)
        return must_stop

    @staticmethod
    def _needs_retry(results: list[SendResult]) -> bool:
        return any(r.outcome is SendOutcome.RETRY for r in results)

    def _record(self, store: StateStore, mapping: MappingResult,
                results: list[SendResult], report: CycleReport) -> None:
        row = mapping.row
        if mapping.outcome is MappingOutcome.NOT_BILLABLE:
            store.record_row(row, RowStatus.NOT_BILLABLE)
            report.not_billable += 1
            return

        if mapping.outcome is MappingOutcome.UNMAPPED:
            store.record_row(row, RowStatus.UNMAPPED, error=mapping.reason)
            store.add_dead_letter(row.id, "unmapped", mapping.reason, _payload(row))
            report.unmapped += 1
            log.warning("unmapped usage row %s: %s", row.id, mapping.reason)
            return

        tokens_sent = {r.event.kind: r.event.tokens for r in results if r.counts_as_sent}
        errors = [r.error for r in results if r.outcome is SendOutcome.REJECTED]
        sub = mapping.external_subscription_id
        if errors:
            error = "; ".join(dict.fromkeys(errors))
            store.record_row(row, RowStatus.FAILED, sub, tokens_sent, error)
            store.add_dead_letter(row.id, "rejected", error, _payload(row))
            report.failed += 1
            log.warning("usage row %s rejected: %s", row.id, error)
        else:
            store.record_row(row, RowStatus.SENT, sub, tokens_sent)
            store.resolve_dead_letter(row.id)    # in case it was a problem before
            report.sent += 1
            report.events_sent += len(results)


def _payload(row: UsageRow) -> dict:
    """The row as stored in the dead-letter table, for debugging and replay."""
    return dataclasses.asdict(row)