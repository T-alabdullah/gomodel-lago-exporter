"""The export loop: read -> map -> build -> send -> record, every few seconds.

Each cycle owns the state database's writer lock and drains durable pending
deliveries before scanning the source overlap window. Before sending anything,
commit the immutable destination and event payloads. After sending, atomically
record acknowledgements, terminal outcomes/dead letters and any cursor movement.

RETRY leaves a pending delivery, without a new terminal usage_rows record, and
stops cursor advancement. Restart uses the saved payload, including after source
or configuration changes. Backfill and send-row share this path without changing
the polling cursor.
"""

import dataclasses
import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from exporter.config import Settings
from exporter.contracts import (
    MappingOutcome, MappingResult, RowStatus, SendOutcome, SendResult, UsageRow,
)
from exporter.events import build_events
from exporter.delivery import Delivery
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
        with self._open_store() as store, store.writer_lock(), store.worker_run("export") as snapshot:
            report = self._run_cycle(store)
            snapshot.update(dataclasses.asdict(report))
            return report

    def _run_cycle(self, store: StateStore) -> CycleReport:
        report = CycleReport()
        report.cursor = store.get_cursor()
        # Pending payloads survive source deletion and moving overlap windows.
        pending_position = None
        while pending := store.pending_deliveries(self._settings.read_batch_size, pending_position):
            if self._process_page(store, [d.row for d in pending], report,
                                  force=True, advance=False):
                return report
            pending_position = pending[-1].row.id
        position = overlap_start(report.cursor, self._settings.overlap_window_seconds)
        while rows := self._reader.read_after(position, self._settings.read_batch_size):
            must_stop = self._process_page(store, rows, report)
            if must_stop:
                break                   # Lago trouble: try again next cycle
            position = Cursor(rows[-1].timestamp, rows[-1].id)
        report.cursor = store.get_cursor()
        return report

    def backfill(self, start: datetime, end: datetime) -> CycleReport:
        """Replay [start, end), keeping the polling cursor completely unchanged."""
        if start.tzinfo is None or end.tzinfo is None or start >= end:
            raise ValueError("Backfill requires timezone-aware start < end.")
        report = CycleReport()
        with self._open_store() as store, store.writer_lock():
            report.cursor = store.get_cursor()
            position = None
            while rows := self._reader.read_after(position, self._settings.read_batch_size,
                                                 since=start, until=end):
                if self._process_page(store, rows, report, force=True, advance=False):
                    break
                position = Cursor(rows[-1].timestamp, rows[-1].id)
        return report

    def send_row(self, row: UsageRow) -> CycleReport:
        """Replay one row through the same durable path as polling and backfill."""
        report = CycleReport()
        with self._open_store() as store, store.writer_lock():
            report.cursor = store.get_cursor()
            self._process_page(store, [row], report, force=True, advance=False)
        return report

    def _process_page(self, store: StateStore, rows: list[UsageRow], report: CycleReport,
                      *, force: bool = False, advance: bool = True) -> bool:
        """Handle one page of rows. Returns True if the cycle must stop (retry later)."""
        report.rows_read += len(rows)
        known = {} if force else store.statuses(r.id for r in rows)
        report.rows_skipped += len(known)
        new_rows = [r for r in rows if r.id not in known]

        mappings = {}
        events = []
        # This transaction MUST commit before HTTP. It pins the subscription,
        # token split, metric codes and timestamp even if a later send crashes.
        with store.transaction():
            for row in new_rows:
                delivery = store.get_delivery(row.id)
                if delivery is None:
                    mapping = store.legacy_mapping(map_row(row, self._settings))
                    if mapping.outcome is MappingOutcome.MAPPED:
                        row_events = store.legacy_events(
                            row.id, build_events(mapping, self._settings, include_zero=True),
                        )
                        delivery = Delivery(mapping.row, mapping.external_subscription_id, row_events)
                if delivery is not None:
                    delivery = store.prepare_delivery(delivery)
                    mapping = MappingResult(delivery.row, MappingOutcome.MAPPED, delivery.subscription_id)
                    events.extend(delivery.events)
                mappings[row.id] = mapping
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
                    store.finish_delivery(row.id, results[row.id], pending=True)
                    report.waiting_for_retry += 1
                    must_stop = True        # leave it unrecorded; don't move past it
                else:
                    store.finish_delivery(row.id, results[row.id], pending=False)
                    self._record(store, mappings[row.id], results[row.id], report)
                if not must_stop:
                    cursor_target = Cursor(row.timestamp, row.id)
            if cursor_target and advance:
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
            store.resolve_dead_letter(row.id)
            report.not_billable += 1
            return

        if mapping.outcome is MappingOutcome.UNMAPPED:
            store.record_row(row, RowStatus.UNMAPPED, error=mapping.reason)
            store.add_dead_letter(row.id, "unmapped", mapping.reason, _payload(row))
            report.unmapped += 1
            log.warning("unmapped usage row %s: %s", row.id, mapping.reason)
            return

        tokens_sent = store.acknowledged_tokens(row.id)
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
