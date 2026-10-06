"""Acceptance helpers inside the demo network. Admin mutations are fixtures only.

Never run against production: these helpers intentionally modify demo source rows.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import signal
import sys
import uuid

import psycopg
from psycopg.types.json import Jsonb
from exporter.cli import _components
from exporter.contracts import MappingOutcome
from exporter.delivery import Delivery
from exporter.events import build_events
from exporter.mapper import map_row
from exporter.runner import Runner
from exporter.sender import Sender


def main(action):
    settings, open_store, reader, client, reconciler = _components()
    runner = Runner(settings, open_store, reader, Sender(client, settings))
    now = datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = now.replace(microsecond=0)
    try:
        if action == 'snapshot':
            source = reconciler.source_rows(start, now+timedelta(seconds=1))
            remote = reconciler.api.events(start, now+timedelta(seconds=1, microseconds=-now.microsecond))
            expected = {}
            mapped = excluded = unmapped = 0
            for row in source.values():
                result = map_row(row, settings)
                if result.outcome is MappingOutcome.MAPPED:
                    mapped += 1
                    for event in build_events(result, settings):
                        expected[event.transaction_id] = event.to_payload()
                elif result.outcome is MappingOutcome.NOT_BILLABLE:
                    excluded += 1
                else:
                    unmapped += 1
            with psycopg.connect(settings.state_db_url) as conn:
                pending = conn.execute('SELECT count(*) FROM deliveries WHERE pending').fetchone()[0]
                counts = dict(conn.execute('SELECT status,count(*) FROM usage_rows GROUP BY status').fetchall())
                dead = conn.execute('SELECT count(*) FROM dead_letters WHERE resolved_at IS NULL').fetchone()[0]
                ack = conn.execute('SELECT count(*) FROM event_acknowledgements').fetchone()[0]
                cursor = conn.execute('SELECT last_timestamp,last_id FROM export_cursor').fetchall()
            return dict(source_rows=len(source), mapped=mapped, excluded=excluded, unmapped=unmapped,
                        expected_events=len(expected), remote_events=len(remote), pending=pending,
                        counts=counts, dead_letters=dead, acknowledgements=ack, cursor=cursor,
                        remote_ids=sorted(e['transaction_id'] for e in remote))
        if action == 'verify':
            source = reconciler.source_rows(start, now)
            # Independent deterministic provider oracle, including cached-token normalization.
            for row in source.values():
                prompt, cached, output = (120, 30, 12) if row.model == 'demo-small' else (240, 60, 24)
                assert row.input_tokens == prompt and row.output_tokens == output, row
                assert row.raw_data.get('prompt_cached_tokens') == cached, row.raw_data
            return {'verified_source_rows': len(source)}
        if action == 'audit':
            return reconciler.run(start, end)
        if action == 'cycle':
            return vars(runner.run_cycle())
        if action == 'backfill':
            return vars(runner.backfill(start, now))
        if action == 'crash':
            original = client.post_batch
            def kill_after_acceptance(events):
                response = original(events)
                response.raise_for_status()
                os.kill(os.getpid(), signal.SIGKILL)
            client.post_batch = kill_after_acceptance
            runner.run_cycle()
            raise AssertionError('Crash hook never sent a real Lago batch')
        if action in ('late', 'older', 'fix-ghost'):
            with psycopg.connect(os.environ['GOMODEL_ADMIN_DB_URL'], autocommit=True) as conn:
                if action == 'fix-ghost':
                    result = conn.execute("UPDATE usage SET labels='[\"lago:sub_acme\"]'::jsonb WHERE provider_name='ollama-qai' AND (labels IS NULL OR labels='[]'::jsonb) AND (user_path IS NULL OR user_path='')")
                    return {'repaired_rows': result.rowcount}
                timestamp = now-timedelta(seconds=30) if action == 'late' else start+timedelta(seconds=1)
                row_id = str(uuid.uuid4())
                # Copy upstream-required fields from a real request; alter only id/time.
                result = conn.execute('''INSERT INTO usage
                    (id, request_id, provider_id, timestamp, model, provider, provider_name,
                     endpoint, user_path, labels, input_tokens, output_tokens, total_tokens, raw_data)
                    SELECT %s, %s, provider_id, %s, model, provider, provider_name,
                     endpoint, user_path, labels, input_tokens, output_tokens, total_tokens, raw_data
                    FROM usage WHERE labels @> '["lago:sub_acme"]'::jsonb AND provider_name='ollama-qai' LIMIT 1''',
                    (row_id, 'acceptance-'+row_id, timestamp))
                assert result.rowcount == 1
                return {'row_id': row_id, 'timestamp': timestamp}
        if action == 'repair-event':
            row_id, kind = sys.argv[2].split(':')
            with psycopg.connect(settings.state_db_url) as conn:
                record = conn.execute('SELECT payload FROM deliveries WHERE usage_row_id=%s', (row_id,)).fetchone()
            event = next(e for e in Delivery.from_dict(record[0]).events if e.kind.value == kind)
            response = client.post_one(event)
            response.raise_for_status()
            return {'restored': event.transaction_id}
        raise ValueError(action)
    finally:
        client.close()


if __name__ == '__main__':
    print(json.dumps(main(sys.argv[1]), default=str))
