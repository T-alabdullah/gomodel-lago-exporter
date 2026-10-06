"""Persistent UTC daily scheduling with bounded catch-up and incomplete-run retry."""

import logging
import threading
from datetime import datetime, timedelta, timezone

from exporter.state.store import ExporterBusyError

log = logging.getLogger(__name__)


class DailyScheduler:
    def __init__(self, settings, open_store, reconciler, now=None):
        self.settings, self.open_store, self.reconciler = settings, open_store, reconciler
        self.now = now or (lambda: datetime.now(timezone.utc))

    def tick(self):
        now = self.now()
        midnight = now.astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        if now < midnight + timedelta(seconds=self.settings.reconciliation_delay_seconds):
            midnight -= timedelta(days=1)
        # Newest first: the current alert remains useful even during long downtime.
        for offset in range(self.settings.reconciliation_lookback_days):
            end = midnight - timedelta(days=offset)
            start = end - timedelta(days=1)
            with self.open_store() as store:
                previous = store.reconciliation_for_period(start, end)
            if previous:
                completed, status = previous
                if (status == 'matched' and completed.date() == now.date()) or (status != 'matched' and (now - completed).total_seconds() < self.settings.reconciliation_retry_seconds):
                    continue
            # Reconciler's global writer lock serializes with delivery. Recheck the
            # period under that lock in run_if_due to prevent competing schedulers.
            return self.reconciler.run_if_due(start, end, now)
        return None

    def run_forever(self, stop: threading.Event):
        while not stop.is_set():
            try:
                self.tick()
            except ExporterBusyError:
                pass
            except Exception:
                log.exception('daily reconciliation failed; retrying on the next scheduler tick')
            stop.wait(min(60, self.settings.reconciliation_retry_seconds))
