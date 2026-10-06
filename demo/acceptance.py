"""Destructive acceptance scenarios for a fresh, isolated demo Compose project only.

Run from repo root after `docker compose --env-file .env.demo up -d --build --wait`.
No host Python dependencies or Docker socket mounted inside application containers.
"""
import json
from pathlib import Path
import subprocess
import time
import urllib.request

COMPOSE = ['docker', 'compose', '--env-file', '.env.demo']
results = []


def compose(*args, check=True):
    result = subprocess.run(COMPOSE+list(args), text=True, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f'Compose {args[:3]} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}')
    return result


def probe(action, *args):
    result = compose('run', '--rm', '--no-deps', 'bootstrap', 'python', '-m', 'demo.probe', action, *args)
    return json.loads(result.stdout.strip().splitlines()[-1])


def record(name, **evidence):
    results.append(dict(scenario=name, passed=True, **evidence))
    Path('acceptance-results.json').write_text(json.dumps(results, indent=2)+'\n')
    print(json.dumps(results[-1]), flush=True)


def eventually(check, timeout=120):
    until = time.monotonic()+timeout
    last = None
    while time.monotonic() < until:
        last = check()
        if last:
            return last
        time.sleep(2)
    raise AssertionError(f'Timed out after {timeout}s: {last}')


def traffic(rounds=1, ghost=True):
    if ghost:
        args = ['python', '-m', 'demo.traffic', '--rounds', str(rounds)]
    else:
        args = ['python', '-c', f"from demo.traffic import run; run({rounds}, customers=('acme','beta'))"]
    result = compose('run', '--rm', '--no-deps', 'traffic', *args)
    return json.loads(result.stdout.strip().splitlines()[-1])['successful_requests']


def drained(expected_rows):
    def ready():
        s = probe('snapshot')
        return s if (s['source_rows'] == expected_rows and not s['pending']
                     and s['expected_events'] == s['remote_events']
                     and sum(s['counts'].values()) == expected_rows) else None
    return eventually(ready)


def audit_matched():
    time.sleep(4)
    report = probe('audit')
    assert report['status'] == 'matched', report
    assert report['billing_periods'], 'No real billing-period comparison ran'
    assert all(t['source_minus_acknowledged'] == 0 and t['acknowledged_minus_lago'] == 0
               for scope in [report, *report['billing_periods']] for t in scope['totals']), report
    return report


def main():
    # A fresh stack is mandatory: never mutate an existing deployment silently.
    assert probe('snapshot')['source_rows'] == 0, 'Acceptance requires empty demo volumes'
    compose('run', '--rm', '--no-deps', 'bootstrap')
    record('bootstrap-rerun-and-readonly-schema-contract')

    rows = traffic(5)
    snap = drained(rows)
    assert snap['unmapped'] == rows//6 and snap['excluded'] == rows//2, snap
    assert snap['dead_letters'] == snap['unmapped'], snap
    probe('verify')
    compose('stop', 'exporter')
    time.sleep(4)
    report = probe('audit')
    assert report['status'] == 'mismatch' and any(i['code'] == 'unmapped' for i in report['issues']), report
    record('mixed-traffic-streaming-cache-and-unmapped', requests=rows, snapshot=snap)
    fixed = probe('fix-ghost')
    assert fixed['repaired_rows'] == rows//6, fixed
    probe('backfill')
    report = audit_matched()
    record('repair-unmapped-and-three-way-zero-difference', report=report)

    before = probe('snapshot')
    probe('backfill')
    probe('backfill')
    after = probe('snapshot')
    assert before == after, (before, after)
    record('repeated-backfill-no-double-billing-no-cursor-change', event_count=after['remote_events'])

    compose('start', 'exporter')
    late = probe('late')
    rows += 1
    assert any(e.startswith(late['row_id']) for e in drained(rows)['remote_ids'])
    record('late-row-inside-overlap', row_id=late['row_id'])
    compose('stop', 'exporter')
    older = probe('older')
    rows += 1
    probe('backfill')
    assert any(e.startswith(older['row_id']) for e in drained(rows)['remote_ids'])
    audit_matched()
    record('older-row-explicit-backfill', row_id=older['row_id'])

    compose('start', 'exporter')
    compose('stop', 'lago-api')
    outage_started = time.monotonic()
    rows += traffic(5, ghost=False)
    time.sleep(max(0, 10-(time.monotonic()-outage_started)))
    # Read persisted state directly; Lago is deliberately inaccessible.
    sql = "SELECT count(*) FROM deliveries WHERE pending"
    pending = compose('exec', '-T', 'exporter-db', 'psql', '-U', 'postgres', '-d', 'exporter', '-Atc', sql).stdout.strip()
    assert int(pending) > 0, pending
    compose('start', 'lago-api')
    recovered = drained(rows)
    record('lago-outage-under-load-and-catchup', source_rows=rows, pending_during_outage=int(pending), recovered=recovered)

    compose('stop', 'exporter')
    before = probe('snapshot')
    rows += traffic(2, ghost=False)
    eventually(lambda: probe('snapshot')['source_rows'] == rows)
    crash = compose('run', '--rm', '--no-deps', 'bootstrap', 'python', '-m', 'demo.probe', 'crash', check=False)
    assert crash.returncode == 137, (crash.returncode, crash.stdout, crash.stderr)
    crashed = probe('snapshot')
    assert crashed['pending'] > 0 and crashed['remote_events'] > before['remote_events'], crashed
    compose('start', 'exporter')
    recovered = drained(rows)
    compose('stop', 'exporter')
    audit_matched()
    record('sigkill-after-real-lago-acceptance-before-local-ack', before=before['remote_events'], after=recovered['remote_events'])

    # Only this isolated demo's Lago data is touched. Restore the immutable event via API.
    tx = recovered['remote_ids'][0]
    assert all(c in '0123456789abcdef-:incachedout' for c in tx)
    deleted = compose('exec', '-T', 'lago-db', 'psql', '-U', 'lago', '-d', 'lago', '-Atc',
        f"DELETE FROM events WHERE transaction_id='{tx}' RETURNING transaction_id").stdout
    assert tx in deleted, deleted
    report = probe('audit')
    assert report['status'] == 'mismatch', report
    assert any(i['code'] == 'missing_lago_event' and tx.split(':')[0] in i['row_ids'] for i in report['issues']), report
    record('deleted-real-lago-event-detected', transaction_id=tx, report=report)
    probe('repair-event', tx)
    audit_matched()
    record('deleted-event-restored-zero-difference')

    # Recreate services with all volumes retained, then check stable identities/counts.
    before = probe('snapshot')
    compose('down')
    compose('up', '-d', '--wait', '--wait-timeout', '600')
    after = drained(rows)
    assert after['remote_ids'] == before['remote_ids'] and after['acknowledgements'] == before['acknowledgements'], (before, after)
    with urllib.request.urlopen('http://127.0.0.1:8000/status') as response:
        status = json.load(response)
    with urllib.request.urlopen('http://127.0.0.1:8000/metrics') as response:
        assert b'exporter_' in response.read()
    with urllib.request.urlopen('http://127.0.0.1:8000/') as response:
        assert b'<html' in response.read().lower()
    record('persistent-restart-and-status-endpoints', final_snapshot=after, status=status)
    print('All real-stack acceptance scenarios passed.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        results.append(dict(scenario='acceptance-failure', passed=False, error=str(error)))
        Path('acceptance-results.json').write_text(json.dumps(results, indent=2)+'\n')
        raise
