"""Read-only status UI, readiness health and low-cardinality Prometheus metrics."""

import html
import json
from datetime import datetime, timezone

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse, Response
from prometheus_client import CollectorRegistry, Gauge, generate_latest, CONTENT_TYPE_LATEST

from exporter.reader import overlap_start
from exporter.state.store import Cursor


class Monitor:
    def __init__(self, settings, open_store, reader, now=None):
        self.settings, self.open_store, self.reader = settings, open_store, reader
        self.now = now or (lambda: datetime.now(timezone.utc))

    def snapshot(self):
        now = self.now()
        try:
            with self.open_store() as store:
                state = store.monitoring_snapshot(now)
                cursor = store.get_cursor()
                oldest = state['oldest_pending']
                position = overlap_start(cursor, self.settings.overlap_window_seconds)
                scanned, backlog = 0, 0
                capped = False
                while True:
                    rows = self.reader.read_after(position, min(self.settings.read_batch_size,
                                                               self.settings.monitor_max_rows - scanned + 1))
                    if not rows:
                        break
                    known = store.statuses(row.id for row in rows)
                    for row in rows:
                        if row.id not in known:
                            backlog += 1
                            oldest = min(oldest, row.timestamp) if oldest else row.timestamp
                    scanned += len(rows)
                    if scanned > self.settings.monitor_max_rows:
                        capped = True
                        break
                    position = Cursor(rows[-1].timestamp, rows[-1].id)
                state.update(now=now, cursor=cursor.timestamp if cursor else None,
                    cursor_lag_seconds=max(0, (now-cursor.timestamp).total_seconds()) if cursor else None,
                    backlog_rows=backlog, backlog_scan_incomplete=capped,
                    oldest_outstanding_age_seconds=max(0, (now-oldest).total_seconds()) if oldest else 0)
                alerts = []
                worker = state['workers'].get('export')
                if not worker:
                    alerts.append('exporter_not_started')
                elif (now - (worker['finished_at'] or worker['started_at'])).total_seconds() > self.settings.heartbeat_stale_seconds:
                    alerts.append('exporter_stale')
                elif worker['succeeded'] is False:
                    alerts.append('exporter_error')
                if state['oldest_outstanding_age_seconds'] > self.settings.lag_alert_seconds:
                    alerts.append('billing_lag')
                if capped:
                    alerts.append('backlog_scan_incomplete')
                if state['counts']['dead_letters_open']:
                    alerts.append('dead_letters')
                latest = state['reconciliation']
                if latest is None:
                    alerts.append('reconciliation_not_started')
                elif latest['status'] != 'matched':
                    alerts.append('reconciliation_' + latest['status'])
                if latest and (now-latest['completed_at']).total_seconds() > 86400 + self.settings.reconciliation_delay_seconds:
                    alerts.append('reconciliation_stale')
                state.update(alerts=alerts, healthy=not alerts, database_available=True)
                return state
        except Exception as error:
            # Never expose exception messages, which can contain connection secrets.
            return dict(now=now, healthy=False, database_available=False,
                        alerts=['database_unavailable'], error=type(error).__name__)


def create_app(monitor: Monitor):
    app = FastAPI(title='GoModel Lago Exporter', docs_url=None, redoc_url=None, openapi_url=None)

    @app.get('/health')
    def health():
        state = monitor.snapshot()
        return JSONResponse(jsonable_encoder(dict(healthy=state['healthy'], alerts=state['alerts'])),
                            status_code=200 if state['healthy'] else 503)

    @app.get('/status')
    def status():
        return JSONResponse(jsonable_encoder(monitor.snapshot()))

    @app.get('/metrics')
    def metrics():
        state = monitor.snapshot()
        registry = CollectorRegistry()
        def gauge(name, description, value):
            if value is not None:
                Gauge('exporter_' + name, description, registry=registry).set(value)
        gauge('healthy', 'Readiness including billing alerts.', int(state['healthy']))
        gauge('database_available', 'Both databases were reachable.', int(state['database_available']))
        if state['database_available']:
            gauge('cursor_lag_seconds', 'Now minus cursor; idle traffic also increases this.', state['cursor_lag_seconds'])
            gauge('oldest_outstanding_age_seconds', 'Age of oldest known outstanding usage.', state['oldest_outstanding_age_seconds'])
            gauge('backlog_scan_incomplete', 'Source backlog scan reached its configured cap.', int(state['backlog_scan_incomplete']))
            gauge('pending_deliveries', 'Durable pending usage rows.', state['pending'])
            gauge('events_acknowledged_today', 'Distinct event identities first observed acknowledged today in UTC.', state['events_acknowledged_today'])
            gauge('dead_letters', 'Open dead-letter rows.', state['counts']['dead_letters_open'])
            gauge('failed_rows', 'Terminal failed usage rows.', state['counts']['failed'])
            gauge('source_backlog_rows', 'Unrecorded rows discovered in source scan; overlaps pending.', state['backlog_rows'])
            worker = state['workers'].get('export')
            gauge('last_run_timestamp_seconds', 'Last completed exporter cycle.',
                  worker['finished_at'].timestamp() if worker and worker['finished_at'] else None)
            latest = state['reconciliation']
            gauge('reconciliation_status', '0 missing, 1 matched, 2 mismatch, 3 incomplete.',
                  {'matched': 1, 'mismatch': 2, 'incomplete': 3}.get(latest['status'], 0) if latest else 0)
            gauge('reconciliation_completed_timestamp_seconds', 'Latest reconciliation completion.',
                  latest['completed_at'].timestamp() if latest else None)
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get('/', response_class=HTMLResponse)
    def home():
        state = monitor.snapshot()
        escape = lambda value: html.escape(str(value))
        cards = ''
        if state['database_available']:
            values = [('Cursor lag (seconds)', state['cursor_lag_seconds']),
                      ('Last completed run', state['workers'].get('export', {}).get('finished_at')),
                      ('Events acknowledged today (UTC)', state['events_acknowledged_today']),
                      ('Pending deliveries', state['pending']), ('Failed rows', state['counts']['failed']),
                      ('Open dead letters', state['counts']['dead_letters_open'])]
            cards = ''.join(f'<article><h2>{escape(label)}</h2><p>{escape(value)}</p></article>' for label,value in values)
        letters = ''.join('<tr>' + ''.join(f'<td>{escape(row.get(key))}</td>' for key in ('usage_row_id','reason','error')) + '</tr>'
                          for row in state.get('dead_letters', []))
        latest = state.get('reconciliation')
        reconciliation = escape(json.dumps(jsonable_encoder(latest), indent=2)) if latest else 'No reconciliation report yet.'
        alerts = ', '.join(state['alerts']) or 'No active alerts'
        return f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30"><title>Usage exporter status</title><style>
body{{font:16px system-ui;margin:40px auto;padding:0 20px;max-width:1100px;background:#f5f7fa;color:#182230}}
h1{{font-size:30px}}h2{{font-size:16px}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:16px}}
article{{background:white;padding:16px;border:1px solid #d4dae2;border-radius:8px}}article p{{font-size:21px;overflow-wrap:anywhere}}
table{{border-collapse:collapse;width:100%;background:white}}th,td{{text-align:left;padding:12px;border:1px solid #d4dae2;overflow-wrap:anywhere}}
pre{{white-space:pre-wrap;overflow-wrap:anywhere;background:white;padding:20px;border:1px solid #d4dae2}}.alert{{padding:14px;background:#e7edf5}}
</style></head><body><h1>GoModel → Lago usage exporter</h1><p class="alert">{escape(alerts)}</p>
<p>Updated {escape(state['now'].isoformat())}. Refreshes every 30 seconds.</p><div class="grid">{cards}</div>
<p>Cursor lag grows during idle periods. Billing-lag alerts use outstanding usage instead.
Events today counts unique acknowledgements, including recovered duplicates, not HTTP attempts.</p>
<h2>Open dead letters (latest 100)</h2><table><thead><tr><th>Usage row</th><th>Reason</th><th>Error</th></tr></thead><tbody>{letters}</tbody></table>
<h2>Latest reconciliation</h2><pre>{reconciliation}</pre><p><a href="/status">JSON status</a> · <a href="/metrics">Prometheus metrics</a> · <a href="/health">Health</a></p></body></html>'''

    return app
