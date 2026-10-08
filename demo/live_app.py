"""Local-only interactive lab. Real GoModel, Ollama, exporter and Lago evidence."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
from typing import Literal
from uuid import UUID, uuid4

import httpx
import psycopg
from psycopg.rows import dict_row
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from exporter.config import get_settings
from exporter.contracts import MappingOutcome
from exporter.delivery import Delivery
from exporter.events import build_events, input_segments
from exporter.lago_client import LagoClient
from exporter.lago_read import LagoReadAPI
from exporter.mapper import map_row
from exporter.monitoring import Monitor
from exporter.reader import UsageReader, _to_usage_row, COLUMNS
from exporter.reconcile import Reconciler
from exporter.runner import Runner
from exporter.scheduler import DailyScheduler
from exporter.sender import Sender
from exporter.state.store import StateStore, ExporterBusyError

MODEL = os.environ.get('LIVE_MODEL', 'llama3.2:1b')
GOMODEL = os.environ.get('GOMODEL_URL', 'http://gomodel:8080')
OLLAMA = os.environ.get('LIVE_OLLAMA_URL', 'http://ollama:11434')
STATIC = Path(__file__).with_name('live_static')


def now():
    return datetime.now(timezone.utc)


class History:
    """Only the lab's request/response journal. Never stores API secrets."""
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS controls (id INTEGER PRIMARY KEY, body TEXT NOT NULL)')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def save(self, record):
        with self.connect() as db:
            db.execute('INSERT INTO requests VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                       (record['id'], json.dumps(record, default=str)))

    def get(self, request_id):
        with self.connect() as db:
            row = db.execute('SELECT body FROM requests WHERE id=?', (request_id,)).fetchone()
        if not row:
            raise HTTPException(404, 'Request not found')
        return json.loads(row[0])

    def recent(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT body FROM requests ORDER BY rowid DESC LIMIT 100')]

    def controls(self, value=None):
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT INTO controls VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                           (json.dumps(value),))
            row = db.execute('SELECT body FROM controls WHERE id=1').fetchone()
        return json.loads(row[0]) if row else {'paused': True, 'network_fault': False}


class Lab:
    def __init__(self):
        self.settings = get_settings()
        self.history = History(Path(os.environ.get('LIVE_DATA_DIR', '/tmp/gomodel-live-lab')) / 'history.sqlite')
        self.control = self.history.controls()
        self.guard = threading.RLock()
        self.stop = threading.Event()
        self.last_cycle = None
        self.worker_error = None
        self.tasks = set()
        self.inference_lock = asyncio.Semaphore(1)

    def store(self):
        return StateStore(self.settings.state_db_url)

    def client(self, fault=False):
        return LagoClient('http://127.0.0.1:1' if fault else self.settings.lago_api_url,
                          self.settings.lago_api_key.get_secret_value(), 15)

    def run(self, row=None, request_id=None):
        with self.guard:
            client = self.client(self.control['network_fault'])
            exchanges = []
            def capture(response):
                response.read()
                def body(raw):
                    try:
                        return json.loads(raw)
                    except (ValueError, UnicodeDecodeError):
                        return 'Non-JSON response'
                if len(exchanges) < 30:
                    exchanges.append({'method': response.request.method, 'path': response.request.url.path,
                        'status': response.status_code, 'observed_at': now().isoformat(),
                        'request_body': body(response.request.content) if response.request.content else None,
                        'response_body': body(response.content)})
            client._http.event_hooks['response'] = [capture]
            settings = self.settings.model_copy(update={'max_retries': 0}) if self.control['network_fault'] else self.settings
            try:
                runner = Runner(settings, self.store, UsageReader(settings.gomodel_db_url), Sender(client, settings))
                report = runner.send_row(row) if row else runner.run_cycle()
                self.last_cycle = {'at': now().isoformat(), **asdict(report), 'summary': report.summary(),
                                   'http_exchanges': exchanges, 'network_fault': self.control['network_fault']}
                if request_id:
                    record = self.history.get(request_id)
                    record['delivery_attempts'] = (record.get('delivery_attempts', []) + [self.last_cycle])[-20:]
                    self.history.save(record)
                self.worker_error = None
                return self.last_cycle
            finally:
                client.close()

    def loop(self):
        last_schedule = 0
        while not self.stop.is_set():
            try:
                if not self.control['paused']:
                    self.run()
                    if time.monotonic() - last_schedule > 60:
                        with self.guard:
                            client = self.client()
                            try:
                                reconciler = Reconciler(self.settings, self.store, UsageReader(self.settings.gomodel_db_url), client)
                                DailyScheduler(self.settings, self.store, reconciler).tick()
                            finally:
                                client.close()
                        last_schedule = time.monotonic()
            except Exception as error:
                self.worker_error = type(error).__name__
            self.stop.wait(self.settings.poll_interval_seconds)

    def source(self, request_id):
        self.history.get(request_id)
        with psycopg.connect(self.settings.gomodel_db_url, autocommit=True, connect_timeout=5) as db:
            rows = db.execute(f'SELECT {COLUMNS} FROM usage WHERE labels ? %s ORDER BY timestamp, id',
                              ('trace:' + request_id,)).fetchall()
        return [_to_usage_row(row) for row in rows]

    def trace(self, request_id):
        record = self.history.get(request_id)
        rows = self.source(request_id)
        local = []
        with psycopg.connect(self.settings.state_db_url, row_factory=dict_row, connect_timeout=5) as db:
            db.execute('SET TRANSACTION READ ONLY')
            for row in rows:
                mapping = map_row(row, self.settings)
                events = build_events(mapping, self.settings) if mapping.outcome is MappingOutcome.MAPPED else []
                local.append({'source': asdict(row), 'mapping': {'outcome': mapping.outcome.value,
                    'subscription': mapping.external_subscription_id, 'reason': mapping.reason},
                    'segments': dict(zip(('uncached', 'cached', 'cache_write'), input_segments(row))),
                    'expected_events': [event.to_payload() for event in events],
                    'delivery': db.execute('SELECT * FROM deliveries WHERE usage_row_id=%s', (row.id,)).fetchone(),
                    'terminal': db.execute('SELECT * FROM usage_rows WHERE usage_row_id=%s', (row.id,)).fetchone(),
                    'acknowledgements': db.execute('SELECT * FROM event_acknowledgements WHERE usage_row_id=%s ORDER BY kind', (row.id,)).fetchall(),
                    'dead_letters': db.execute('SELECT * FROM dead_letters WHERE usage_row_id=%s ORDER BY id', (row.id,)).fetchall()})
        remote, remote_error = [], None
        if rows:
            client = self.client()
            try:
                start = min(r.timestamp for r in rows).replace(microsecond=0) - timedelta(seconds=1)
                end = max(r.timestamp for r in rows).replace(microsecond=0) + timedelta(seconds=2)
                identities = {r.id for r in rows}
                remote = [e for e in LagoReadAPI(client, self.settings).events(start, end)
                          if e.get('transaction_id', '').rsplit(':', 1)[0] in identities]
            except Exception as error:
                remote_error = type(error).__name__
            finally:
                client.close()
        return {'request': record, 'rows': local, 'lago_events': remote, 'lago_error': remote_error,
                'observed_at': now(), 'control': self.control}

    async def generate(self, record):
        secret = None
        async with self.inference_lock:
            record.update(status='generating', started_at=now().isoformat())
            self.history.save(record)
            try:
                async with httpx.AsyncClient(base_url=GOMODEL, timeout=180) as client:
                    labels = ['trace:' + record['id']]
                    if record['customer'] == 'acme':
                        labels.append('lago:sub_acme')
                    spec = {'name': 'live-' + record['id'], 'labels': labels}
                    if record['customer'] == 'beta':
                        spec['user_path'] = '/customers/sub_beta'
                    response = await client.post('/admin/auth-keys', json=spec,
                        headers={'Authorization': 'Bearer ' + os.environ['GOMODEL_MASTER_KEY']})
                    response.raise_for_status()
                    secret = response.json()['value']
                    record['key_metadata'] = spec
                    self.history.save(record)
                    done, last_save = False, 0
                    async with client.stream('POST', '/v1/chat/completions', json=record['http_body'],
                                             headers={'Authorization': 'Bearer ' + secret}) as response:
                        record['http_status'] = response.status_code
                        response.raise_for_status()
                        async for line in response.aiter_lines():
                            if line == 'data: [DONE]':
                                done = True
                            elif line.startswith('data: '):
                                chunk = json.loads(line[6:])
                                record['response_id'] = chunk.get('id', record.get('response_id'))
                                if chunk.get('usage'):
                                    record['usage'] = chunk['usage']
                                for choice in chunk.get('choices', []):
                                    record['answer'] += choice.get('delta', {}).get('content') or ''
                                    if choice.get('finish_reason'):
                                        record['finish_reason'] = choice['finish_reason']
                            if time.monotonic() - last_save > .15:
                                self.history.save(record)
                                last_save = time.monotonic()
                    if not done:
                        raise ValueError('Stream ended without DONE')
                    record['status'] = 'completed' if record.get('usage') else 'usage_missing'
            except asyncio.CancelledError:
                record.update(status='interrupted', error='Server stopped; inspect source usage before retrying.')
                raise
            except Exception as error:
                record.update(status='error', error=type(error).__name__ + ': inspect GoModel/Ollama service health')
            finally:
                secret = None
                record['finished_at'] = now().isoformat()
                self.history.save(record)


class ChatInput(BaseModel):
    prompt: str = Field(min_length=1, max_length=4000)
    customer: Literal['acme', 'beta', 'ghost'] = 'acme'
    provider: Literal['ollama-qai', 'ollama-ext'] = 'ollama-qai'
    max_tokens: int = Field(default=160, ge=8, le=512)
    temperature: float = Field(default=.5, ge=0, le=1.5)


class ControlInput(BaseModel):
    paused: bool
    network_fault: bool


@asynccontextmanager
async def lifespan(app):
    lab = Lab()
    app.state.lab = lab
    with lab.store() as store:
        store.init_schema()
    for record in lab.history.recent():
        if record['status'] in ('queued', 'generating'):
            record.update(status='interrupted', error='Server restarted; inspect source usage before retrying.')
            lab.history.save(record)
    thread = threading.Thread(target=lab.loop, daemon=True)
    thread.start()
    yield
    lab.stop.set()
    for task in lab.tasks:
        task.cancel()
    await asyncio.gather(*lab.tasks, return_exceptions=True)
    await asyncio.to_thread(thread.join, 20)


app = FastAPI(title='Llama Usage Lab', lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost', '127.0.0.1', 'live-lab', 'testserver'])


@app.middleware('http')
async def local_write_guard(request, call_next):
    # A hostile website cannot invoke privileged local demo operations.
    if request.method not in ('GET', 'HEAD', 'OPTIONS'):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'detail': 'Cross-origin write blocked'}, status_code=403)
        if request.headers.get('x-lab-action') != '1':
            return JSONResponse({'detail': 'Missing local action header'}, status_code=403)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
    return response


@app.exception_handler(ExporterBusyError)
async def busy_handler(request, error):
    return JSONResponse({'detail': 'Exporter is busy; retry after the current cycle.'}, status_code=409)


@app.get('/')
def home():
    return FileResponse(STATIC / 'index.html')


@app.get('/api/ping')
def ping():
    return {'ok': True}


@app.get('/api/settings')
def settings(request: Request):
    lab = request.app.state.lab
    cfg = lab.settings
    return {'model': MODEL, 'runtime': 'Ollama / local CPU inference',
        'routes': {'browser': 'localhost:8090', 'gomodel': GOMODEL, 'ollama': OLLAMA, 'lago': cfg.lago_api_url},
        'source_database': {'host': 'gomodel-db', 'database': 'gomodel', 'role': 'exporter_ro', 'access': 'SELECT usage'},
        'state_database': {'host': 'exporter-db', 'database': 'exporter'},
        'policy': {'billable_providers': cfg.billable_providers, 'mapping_order': cfg.mapping_order,
                   'bill_cache_hits': cfg.bill_cache_hits, 'cache_write_billing': cfg.cache_write_billing,
                   'overlap_seconds': cfg.overlap_window_seconds, 'poll_seconds': cfg.poll_interval_seconds,
                   'batch_size': cfg.lago_batch_size, 'reconciliation_grace_seconds': cfg.reconciliation_delay_seconds},
        'metrics': [cfg.metric_input, cfg.metric_cached_input, cfg.metric_output],
        'control': lab.control, 'pricing_note': 'Demo USD package pricing, rounded up per million tokens; not pro-rata.',
        'correlation': 'A dedicated API key carries trace:<request UUID>. Secrets stay on the server.',
        'history': 'Last 100 requests shown. SQLite journal persists in live-history volume.'}


@app.get('/api/status')
def status(request: Request):
    lab = request.app.state.lab
    data = Monitor(lab.settings, lab.store, UsageReader(lab.settings.gomodel_db_url)).snapshot()
    return {'billing': data, 'control': lab.control, 'last_cycle': lab.last_cycle, 'worker_error': lab.worker_error}


@app.get('/api/services')
async def services():
    async def check(name, url):
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(url)
                return {'name': name, 'ok': response.is_success, 'status': response.status_code}
        except httpx.HTTPError:
            return {'name': name, 'ok': False}
    return await asyncio.gather(check('GoModel', GOMODEL + '/health'),
                               check('Ollama', OLLAMA + '/api/tags'),
                               check('Lago', get_settings().lago_api_url + '/health'))


@app.get('/api/requests')
def requests(request: Request):
    return request.app.state.lab.history.recent()


@app.post('/api/requests', status_code=202)
async def chat(body: ChatInput, request: Request):
    lab = request.app.state.lab
    if len(lab.tasks) >= 3:
        raise HTTPException(429, 'Three requests already queued; wait for inference to finish.')
    record = {'id': str(uuid4()), 'created_at': now().isoformat(), 'status': 'queued',
              **body.model_dump(), 'model': MODEL, 'answer': '',
              'http_body': {'model': body.provider + '/' + MODEL, 'messages': [{'role': 'user', 'content': body.prompt}],
                            'stream': True, 'stream_options': {'include_usage': True},
                            'max_tokens': body.max_tokens, 'temperature': body.temperature}}
    lab.history.save(record)
    task = asyncio.create_task(lab.generate(record))
    lab.tasks.add(task)
    task.add_done_callback(lab.tasks.discard)
    return record


@app.get('/api/requests/{request_id}')
def trace(request_id: UUID, request: Request):
    return request.app.state.lab.trace(str(request_id))


@app.post('/api/requests/{request_id}/prepare')
def prepare(request_id: UUID, request: Request):
    lab = request.app.state.lab
    rows = lab.source(str(request_id))
    if not rows:
        raise HTTPException(409, 'GoModel has not persisted usage yet.')
    prepared = []
    with lab.guard, lab.store() as store, store.writer_lock(), store.transaction():
        for row in rows:
            mapping = map_row(row, lab.settings)
            if mapping.outcome is MappingOutcome.MAPPED:
                prepared.append(store.prepare_delivery(Delivery(row, mapping.external_subscription_id,
                    build_events(mapping, lab.settings))).to_dict())
    return {'prepared': prepared, 'note': f'{len(prepared)} durable delivery intent(s) committed. No HTTP event send.'}


@app.post('/api/requests/{request_id}/replay')
def replay(request_id: UUID, request: Request):
    lab = request.app.state.lab
    rows = lab.source(str(request_id))
    if not rows:
        raise HTTPException(409, 'GoModel has not persisted usage yet.')
    return {'reports': [lab.run(row, str(request_id)) for row in rows]}


@app.post('/api/requests/{request_id}/reconcile')
def reconcile(request_id: UUID, request: Request):
    lab = request.app.state.lab
    rows = lab.source(str(request_id))
    if not rows:
        raise HTTPException(409, 'No persisted source rows to audit.')
    start = min(r.timestamp for r in rows).replace(microsecond=0)
    end = max(r.timestamp for r in rows).replace(microsecond=0) + timedelta(seconds=1)
    with lab.guard:
        client = lab.client()
        try:
            return Reconciler(lab.settings, lab.store, UsageReader(lab.settings.gomodel_db_url), client).run(start, end)
        finally:
            client.close()


@app.post('/api/control')
def control(body: ControlInput, request: Request):
    lab = request.app.state.lab
    with lab.guard:
        lab.control = body.model_dump()
        lab.history.controls(lab.control)
    return lab.control


@app.post('/api/cycle')
def cycle(request: Request):
    return request.app.state.lab.run()


TABLES = {'source': ('usage',), 'state': ('deliveries', 'usage_rows', 'event_acknowledgements',
          'dead_letters', 'export_cursor', 'worker_status', 'reconciliation_runs')}
ORDERS = {'usage': 'timestamp DESC, id DESC', 'deliveries': 'updated_at DESC', 'usage_rows': 'updated_at DESC',
          'event_acknowledgements': 'first_ack_at DESC', 'dead_letters': 'id DESC', 'export_cursor': 'updated_at DESC',
          'worker_status': 'started_at DESC', 'reconciliation_runs': 'id DESC'}


@app.get('/api/database/{database}/{table}')
def database(database: str, table: str, request: Request):
    if table not in TABLES.get(database, ()):
        raise HTTPException(404, 'Unknown database or table')
    cfg = request.app.state.lab.settings
    dsn = cfg.gomodel_db_url if database == 'source' else cfg.state_db_url
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as db:
        db.execute('SET TRANSACTION READ ONLY')
        columns = db.execute("SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position", (table,)).fetchall()
        # Identifiers are drawn exclusively from the fixed allowlist above.
        total = db.execute(f'SELECT count(*) AS count FROM {table}').fetchone()['count']
        rows = db.execute(f'SELECT * FROM {table} ORDER BY {ORDERS[table]} LIMIT 100').fetchall()
    return {'database': database, 'table': table, 'columns': columns, 'total': total, 'rows': rows,
            'query': f'SELECT * FROM {table} ORDER BY {ORDERS[table]} LIMIT 100;', 'read_only': True}


@app.get('/api/lago')
def lago(request: Request):
    lab = request.app.state.lab
    client = lab.client()
    try:
        api = LagoReadAPI(client, lab.settings)
        return {'metrics': api.get('/api/v1/billable_metrics'), 'plan': api.get('/api/v1/plans/llm_usage'),
                'subscriptions': {sub: api.subscription(sub) for sub in ('sub_acme', 'sub_beta')},
                'usage': {sub: api.current_model_usage(api.subscription(sub), MODEL) for sub in ('sub_acme', 'sub_beta')}}
    except Exception as error:
        raise HTTPException(503, 'Lago evidence unavailable: ' + type(error).__name__)
    finally:
        client.close()


app.mount('/static', StaticFiles(directory=STATIC, check_dir=False), name='static')
