"""Bounded concurrent mixed traffic; a successful run means every request succeeded."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
import httpx

MODELS = ('demo-small', 'demo-large')


def request(client, secret, model, provider, stream):
    body = dict(model=f'{provider}/{model}', messages=[{'role': 'user', 'content': 'Reply briefly.'}],
                max_tokens=32, stream=stream)
    headers = {'Authorization': f'Bearer {secret}'}
    if stream:
        body['stream_options'] = {'include_usage': True}
        usage, done = None, False
        with client.stream('POST', '/v1/chat/completions', json=body, headers=headers) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if line == 'data: [DONE]':
                    done = True
                elif line.startswith('data: '):
                    usage = json.loads(line[6:]).get('usage') or usage
        assert done and usage, 'Streaming response did not finish with usage'
    else:
        response = client.post('/v1/chat/completions', json=body, headers=headers)
        response.raise_for_status()
        usage = response.json()['usage']
    expected = (120, 12) if model == 'demo-small' else (240, 24)
    assert (usage['prompt_tokens'], usage['completion_tokens']) == expected, usage
    return 1


def run(rounds=1, customers=('acme', 'beta', 'ghost'), concurrency=6, pause=0.0):
    keys = json.loads(Path(os.environ.get('GOMODEL_KEYS_FILE', '.gomodel-keys.json')).read_text())
    count = 0
    with httpx.Client(base_url=os.environ.get('GOMODEL_URL', 'http://localhost:8080'), timeout=30) as client:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            for _ in range(rounds):
                futures = [pool.submit(request, client, keys[name], model, provider, stream)
                           for name in customers for model in MODELS
                           for provider in ('ollama-qai', 'ollama-ext') for stream in (False, True)]
                count += sum(f.result() for f in futures)
                if pause:
                    time.sleep(pause)
    print(json.dumps({'successful_requests': count, 'rounds': rounds, 'customers': list(customers)}), flush=True)
    return count


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--rounds', type=int, default=10)
    parser.add_argument('--concurrency', type=int, default=6)
    parser.add_argument('--pause', type=float, default=0)
    args = parser.parse_args()
    assert args.rounds > 0 and args.concurrency > 0 and args.pause >= 0
    run(args.rounds, concurrency=args.concurrency, pause=args.pause)
