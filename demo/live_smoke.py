"""Bounded real-stack verification. Creates four labelled Llama requests."""
import json
import time

import httpx


def main():
    with httpx.Client(base_url='http://localhost:8090', timeout=90, headers={'X-Lab-Action':'1'}) as client:
        def get(path):
            response = client.get(path)
            response.raise_for_status()
            return response.json()

        def post(path, body):
            response = client.post(path, json=body)
            response.raise_for_status()
            return response.json()

        def trace(request_id):
            return get('/api/requests/' + request_id)

        def generate(**kwargs):
            record = post('/api/requests', {'prompt':'Say hello in one short sentence.', 'max_tokens':24, **kwargs})
            deadline = time.monotonic() + 150
            while time.monotonic() < deadline:
                data = trace(record['id'])
                if data['request']['status'] in ('error', 'interrupted', 'usage_missing'):
                    raise AssertionError(data['request'])
                if data['request']['status'] == 'completed' and data['rows']:
                    assert data['request']['answer']
                    assert data['rows'][0]['source']['model'] == 'llama3.2:1b'
                    return record['id']
                time.sleep(1)
            raise AssertionError('Inference/persistence timed out')

        previous = get('/api/settings')['control']
        report = {}
        try:
            post('/api/control', {'paused':True, 'network_fault':False})
            request_id = generate()
            base = '/api/requests/' + request_id
            assert trace(request_id)['lago_events'] == []
            post(base + '/prepare', {})
            prepared_state = trace(request_id)['rows'][0]['delivery']
            prepared = prepared_state['payload']
            assert prepared_state['pending'] is True
            post(base + '/replay', {})
            delivered = trace(request_id)
            assert delivered['rows'][0]['terminal']['status'] == 'sent'
            expected = {e['transaction_id'] for e in delivered['rows'][0]['expected_events']}
            assert {e['transaction_id'] for e in delivered['lago_events']} == expected
            post(base + '/replay', {})
            repeated = trace(request_id)
            assert len(repeated['lago_events']) == len(expected)
            assert len(repeated['rows'][0]['acknowledgements']) == len(expected)
            assert repeated['rows'][0]['delivery']['payload'] == prepared
            report['mapped_and_duplicate_replay'] = request_id
            print('PASS real Llama → source → immutable delivery → Lago → duplicate replay', flush=True)

            request_id = generate(customer='beta')
            base = '/api/requests/' + request_id
            post('/api/control', {'paused':True, 'network_fault':True})
            post(base + '/replay', {})
            failed = trace(request_id)
            assert failed['rows'][0]['delivery']['pending'] is True
            assert failed['rows'][0]['terminal'] is None
            saved = failed['rows'][0]['delivery']['payload']
            post('/api/control', {'paused':True, 'network_fault':False})
            post(base + '/replay', {})
            recovered = trace(request_id)
            assert recovered['rows'][0]['terminal']['external_subscription_id'] == 'sub_beta'
            assert recovered['rows'][0]['delivery']['payload'] == saved
            assert recovered['rows'][0]['delivery']['pending'] is False
            report['beta_and_outage_recovery'] = request_id
            print('PASS user-path mapping and real retry recovery', flush=True)

            for kwargs, expected_status in [({'provider':'ollama-ext'}, 'not_billable'), ({'customer':'ghost'}, 'unmapped')]:
                request_id = generate(**kwargs)
                post('/api/requests/' + request_id + '/replay', {})
                data = trace(request_id)
                assert data['rows'][0]['terminal']['status'] == expected_status
                assert data['lago_events'] == []
                if expected_status == 'unmapped':
                    assert len(data['rows'][0]['dead_letters']) == 1
                report[expected_status] = request_id
                print('PASS ' + expected_status + ' persisted without Lago events', flush=True)

            # Includes the user's prior pending tutorial row; uses normal production cycle.
            post('/api/cycle', {})
            time.sleep(3)
            audit = post('/api/requests/' + report['mapped_and_duplicate_replay'] + '/reconcile', {})
            assert audit['status'] == 'matched', audit['issues']
            report['reconciliation'] = audit['status']
            print('PASS independent reconciliation matched', flush=True)
            for path in ['/api/database/source/usage', '/api/database/state/deliveries',
                         '/api/database/state/reconciliation_runs', '/api/lago', '/api/status', '/api/settings']:
                assert get(path)
            print(json.dumps(report, indent=2), flush=True)
        finally:
            post('/api/control', previous)


if __name__ == '__main__':
    main()
