"""Start the real local Llama lab: python3 scripts/run_live.py."""
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '--env-file', '.env.demo', '-f', 'docker-compose.yml', '-f', 'docker-compose.live.yml']


def run(*args, env=None):
    subprocess.run(args, cwd=ROOT, env=env, check=True)


def main():
    run(sys.executable, 'scripts/init_demo.py')
    run('docker', 'info', '--format', 'Docker engine: {{.Architecture}}')
    run('docker', 'build', '-t', 'gomodel-exporter:local', '.')
    run(*COMPOSE, 'build', 'live-lab')
    # Use one exporter writer. This preserves all source and state volumes.
    run(*COMPOSE, 'stop', 'exporter')
    run(*COMPOSE, 'up', '-d', '--wait', '--wait-timeout', '600', 'ollama', 'gomodel-db',
        'exporter-db', 'lago-api', 'lago-worker', 'lago-clock')
    run(*COMPOSE, 'exec', '-T', 'ollama', 'ollama', 'pull', 'llama3.2:1b')
    run(*COMPOSE, 'up', '-d', '--wait', '--wait-timeout', '180', 'gomodel')
    # Secrets are passed through environment, not printed or supplied as CLI values.
    values = dict(line.split('=', 1) for line in (ROOT / '.env.demo').read_text().splitlines()
                  if line and not line.startswith('#'))
    env = dict(os.environ)
    env['READONLY_PASSWORD'] = values['READONLY_PASSWORD']
    env['GOMODEL_ADMIN_DB_URL'] = 'postgresql://postgres:' + values['GOMODEL_PASSWORD'] + '@gomodel-db:5432/gomodel'
    run(*COMPOSE, 'run', '--rm', '--no-deps', '-e', 'GOMODEL_ADMIN_DB_URL', '-e', 'READONLY_PASSWORD',
        'live-lab', 'python', '-m', 'demo.live_setup', env=env)
    run(*COMPOSE, 'up', '-d', '--no-deps', '--wait', 'live-lab')
    print('\nOpen http://localhost:8090\nDelivery starts paused on first launch. Settings persist on restart.')


if __name__ == '__main__':
    main()
