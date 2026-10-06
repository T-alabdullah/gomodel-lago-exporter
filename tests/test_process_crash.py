"""Kill a real exporter process after HTTP acceptance, then recover from PostgreSQL."""

import json
import multiprocessing
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

from exporter.lago_client import LagoClient
from exporter.reader import UsageReader
from exporter.runner import Runner
from exporter.sender import Sender
from exporter.state.store import StateStore
from tests.factories import T0, settings
from tests.test_runner import (
    _databases, gomodel, state, lago, insert, make_runner, status_of,
    _url, STATE_DB, GOMODEL_DB,
)


def run_child(base_url, state_dsn, source_dsn):
    config = settings(max_retries=0)
    client = LagoClient(base_url, 'test-key', timeout=30)
    try:
        Runner(config, lambda: StateStore(state_dsn), UsageReader(source_dsn),
               Sender(client, config)).run_cycle()
    finally:
        client.close()


def test_sigkill_after_remote_acceptance_keeps_original_payload(gomodel, state, lago):
    row = insert(gomodel, T0)
    accepted = threading.Event()
    release = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.handle_request()

        def do_POST(self):
            self.handle_request()

        def handle_request(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            result = lago._handle(httpx.Request(self.command, 'http://lago.test' + self.path, content=body))
            if self.command == 'POST':
                accepted.set()  # Lago has stored the events; client hasn't received an answer.
                release.wait(20)
            try:
                self.send_response(result.status_code)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(result.content)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    process = multiprocessing.get_context('spawn').Process(
        target=run_child,
        args=(f'http://127.0.0.1:{server.server_port}', _url(STATE_DB), _url(GOMODEL_DB)),
    )
    try:
        process.start()
        assert accepted.wait(20), 'child did not reach remote acceptance'
        process.kill()
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
        assert len(lago.stored) == 2
        assert status_of(state, row) is None
        assert len(state.pending_deliveries(10)) == 1
        gomodel.execute('UPDATE usage SET labels = %s, input_tokens = 900 WHERE id = %s',
                        (json.dumps(['lago:sub_beta']), row))
        make_runner(lago).run_cycle()
        assert len(lago.stored) == 2
        assert {key[0] for key in lago.stored} == {'sub_acme'}
        assert status_of(state, row).value == 'sent'
        assert not state.pending_deliveries(10)
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)
