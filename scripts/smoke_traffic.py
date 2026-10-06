"""Send a few chat completions through GoModel with every test key.

For each key: one non-streaming and one streaming request to the billable
provider (ollama-qai), and one request to the non-billable one (ollama-ext).

Usage:
    python scripts/smoke_traffic.py
"""

import json
import os
import sys
from pathlib import Path

import httpx

GOMODEL_URL = os.environ.get("GOMODEL_URL", "http://localhost:8080")
MODEL = os.environ.get("SMOKE_MODEL", "demo-small")
KEYS_FILE = Path(__file__).resolve().parent.parent / ".gomodel-keys.json"

REQUESTS = [
    ("ollama-qai", False),
    ("ollama-qai", True),
    ("ollama-ext", False),
]


def chat(client: httpx.Client, key: str, provider: str, stream: bool) -> str:
    body = {
        "model": f"{provider}/{MODEL}",  # provider-qualified: picks the exact provider
        "messages": [{"role": "user", "content": "Reply with one short word."}],
        "max_tokens": 16,
        "stream": stream,
    }
    headers = {"Authorization": f"Bearer {key}"}
    if not stream:
        r = client.post("/v1/chat/completions", json=body, headers=headers)
        r.raise_for_status()
        usage = r.json().get("usage", {})
        return f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out"

    with client.stream("POST", "/v1/chat/completions", json=body, headers=headers) as r:
        r.raise_for_status()
        usage = {}
        for line in r.iter_lines():
            if line.startswith("data: ") and line != "data: [DONE]":
                usage = json.loads(line[6:]).get("usage") or usage
        return f"{usage.get('prompt_tokens')} in / {usage.get('completion_tokens')} out"


def main() -> None:
    if not KEYS_FILE.exists():
        sys.exit("No .gomodel-keys.json yet: run scripts/gomodel_setup.py first.")
    keys = json.loads(KEYS_FILE.read_text())

    with httpx.Client(base_url=GOMODEL_URL, timeout=120) as client:
        for name, key in keys.items():
            for provider, stream in REQUESTS:
                mode = "stream" if stream else "plain "
                print(f"  {name:6} {provider:11} {mode}  {chat(client, key, provider, stream)}")
    print("Done. Usage rows appear in Postgres within ~5 seconds.")


if __name__ == "__main__":
    main()