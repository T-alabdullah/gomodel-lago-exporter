"""Fixed, demo-only reset coordinator. No network listener or arbitrary commands.

Only this helper receives the Docker socket; the web application does not.
The journal volume carries a reset request and progress, surviving lab restarts.
"""
import json
import os
from pathlib import Path
import sqlite3
import time

import httpx

DATA = Path(os.environ.get('LIVE_DATA_DIR', '/home/exporter/live-data'))
PROJECT = os.environ.get('LIVE_COMPOSE_PROJECT', 'gomodel-lago-exporter')
SERVICES = ('live-lab', 'exporter', 'gomodel', 'gomodel-db', 'exporter-db',
            'lago-db', 'lago-redis', 'lago-migrate', 'lago-api', 'lago-worker', 'lago-clock')


def write_status(state, message):
    path = DATA / 'reset-status.json'
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps({'state': state, 'message': message}))
    temporary.replace(path)


class Docker:
    def __init__(self):
        self.http = httpx.Client(transport=httpx.HTTPTransport(uds='/var/run/docker.sock'),
                                 base_url='http://docker', timeout=180)
        version = self.call('GET', '/version')['ApiVersion']
        self.http.base_url = f'http://docker/v{version}/'
        containers = self.call('GET', '/containers/json', params={'all': 'true', 'filters': json.dumps({
            'label': [f'com.docker.compose.project={PROJECT}', 'com.docker.compose.oneoff=False']})})
        self.ids = {}
        for container in containers:
            service = container['Labels'].get('com.docker.compose.service')
            if service in SERVICES:
                if service in self.ids:
                    raise RuntimeError('Reset requires one container per demo service')
                self.ids[service] = container['Id']
        if set(SERVICES) - {'exporter'} - self.ids.keys():
            raise RuntimeError('Demo containers are missing; rerun scripts/run_live.py')

    def call(self, method, path, **kwargs):
        response = self.http.request(method, path.lstrip('/'), **kwargs)
        if response.status_code == 304:  # Already stopped/started: idempotent retry.
            return None
        response.raise_for_status()
        return response.json() if response.content else None

    def stop(self, service):
        if service in self.ids:
            self.call('POST', f'/containers/{self.ids[service]}/stop', params={'t': 30})

    def start(self, service):
        self.call('POST', f'/containers/{self.ids[service]}/start')

    def wait(self, service, completed=False):
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            state = self.call('GET', f'/containers/{self.ids[service]}/json')['State']
            if completed and state['Status'] == 'exited':
                if state['ExitCode'] != 0:
                    raise RuntimeError(f'{service} failed; inspect its container logs')
                return
            if not completed and state.get('Health', {}).get('Status') == 'healthy':
                return
            time.sleep(2)
        raise RuntimeError(f'{service} did not become ready')

    def execute(self, service, command, env=None):
        result = self.call('POST', f'/containers/{self.ids[service]}/exec', json={
            'Cmd': command, 'Env': env or [], 'AttachStdout': False, 'AttachStderr': False})
        ident = result['Id']
        self.call('POST', f'/exec/{ident}/start', json={'Detach': True})
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            result = self.call('GET', f'/exec/{ident}/json')
            if not result['Running']:
                if result['ExitCode'] != 0:
                    raise RuntimeError(f'Reset operation failed in {service}')
                return
            time.sleep(1)
        raise RuntimeError(f'Reset operation timed out in {service}')


def reset(docker):
    write_status('running', 'Stopping demo writers…')
    for service in ('live-lab', 'exporter', 'gomodel', 'lago-clock', 'lago-worker', 'lago-api', 'lago-migrate'):
        docker.stop(service)
    # Stop all writers before deleting data so buffered usage/jobs cannot return.
    write_status('running', 'Clearing usage, delivery state, and Lago billing…')
    docker.execute('gomodel-db', ['psql', '-U', 'postgres', '-d', 'gomodel', '-v', 'ON_ERROR_STOP=1',
                                  '-c', 'TRUNCATE TABLE usage;'])
    docker.execute('exporter-db', ['psql', '-U', 'postgres', '-d', 'exporter', '-v', 'ON_ERROR_STOP=1',
        '-c', 'TRUNCATE TABLE export_cursor, usage_rows, dead_letters, deliveries, '
              'event_acknowledgements, worker_status, reconciliation_runs RESTART IDENTITY;'])
    docker.execute('lago-redis', ['redis-cli', 'FLUSHALL'])
    docker.execute('lago-db', ['psql', '-U', 'lago', '-d', 'postgres', '-v', 'ON_ERROR_STOP=1',
                              '-c', 'DROP DATABASE IF EXISTS lago WITH (FORCE);',
                              '-c', 'CREATE DATABASE lago OWNER lago;'])
    with sqlite3.connect(DATA / 'history.sqlite') as db:
        db.execute('DELETE FROM requests')
        db.execute('INSERT OR REPLACE INTO controls VALUES (1, ?)',
                   (json.dumps({'paused': True, 'network_fault': False}),))
    write_status('running', 'Recreating Lago and restoring demo configuration…')
    docker.start('lago-migrate')
    docker.wait('lago-migrate', completed=True)
    for service in ('gomodel', 'lago-api', 'lago-worker'):
        docker.start(service)
        docker.wait(service)
    docker.start('live-lab')
    docker.wait('live-lab')
    docker.execute('live-lab', ['python', '-m', 'demo.live_setup'], env=[
        'GOMODEL_ADMIN_DB_URL=' + os.environ['GOMODEL_ADMIN_DB_URL'],
        'READONLY_PASSWORD=' + os.environ['READONLY_PASSWORD']])
    docker.start('lago-clock')
    write_status('complete', 'Fresh demo ready. Automatic delivery is paused; fault injection is off.')


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    # An interrupted reset is never silently resumed or treated as successful.
    progress = DATA / 'reset-status.json'
    if progress.exists() and json.loads(progress.read_text()).get('state') == 'running':
        write_status('failed', 'Reset interrupted. Retry Fresh start after restarting the demo.')
    while True:
        (DATA / 'reset-heartbeat').touch()
        request = DATA / 'reset-request'
        if request.exists():
            request.unlink()
            docker = None
            try:
                docker = Docker()
                reset(docker)
            except Exception as error:
                # Do not include Docker request bodies, credentials, or raw responses.
                write_status('failed', f'Reset failed ({type(error).__name__}). Rerun scripts/run_live.py, then retry Fresh start.')
                if docker is not None:
                    try:
                        docker.start('live-lab')  # Expose the failure to the browser.
                    except Exception:
                        pass
            finally:
                if docker is not None:
                    docker.http.close()
        time.sleep(1)


if __name__ == '__main__':
    main()
