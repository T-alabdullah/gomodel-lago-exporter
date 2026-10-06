"""Create the test API keys in GoModel (safe to run more than once).

Each key represents one kind of customer the exporter must handle:

    acme   -> label "lago:sub_acme"           mapped by label            (D2)
    beta   -> user_path "/customers/sub_beta" mapped by user_path         (D2, D3)
    ghost  -> nothing                          unmapped -> dead letter

GoModel only shows a key's secret once, when it is created, so the secrets
are saved to .gomodel-keys.json (git-ignored) for the traffic scripts.

Usage:
    python scripts/gomodel_setup.py
"""

import json
import os
import sys
import time
import tempfile
from pathlib import Path

import httpx

GOMODEL_URL = os.environ.get("GOMODEL_URL", "http://localhost:8080")
MASTER_KEY = os.environ.get("GOMODEL_MASTER_KEY", "change-me")
KEYS_FILE = Path(os.environ.get("GOMODEL_KEYS_FILE", str(Path(__file__).resolve().parent.parent / ".gomodel-keys.json")))

TEST_KEYS = [
    {"name": "acme", "labels": ["lago:sub_acme"]},
    {"name": "beta", "user_path": "/customers/sub_beta"},
    {"name": "ghost"},
]


def wait_for_gomodel(client: httpx.Client, timeout: float = 120) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if client.get("/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(2)
    sys.exit(f"GoModel did not become healthy at {GOMODEL_URL}")


def main() -> None:
    saved = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}
    headers = {"Authorization": f"Bearer {MASTER_KEY}"}

    with httpx.Client(base_url=GOMODEL_URL, headers=headers, timeout=30) as client:
        wait_for_gomodel(client)
        response = client.get("/admin/auth-keys")
        response.raise_for_status()
        existing = {k["name"]: k for k in response.json() if k.get("active")}

        for spec in TEST_KEYS:
            name = spec["name"]
            if name in existing and name in saved:
                print(f"  {name}: already exists")
                continue
            if name in existing:
                print(f"  {name}: exists in GoModel but its secret was not saved here.")
                print(f"         Deactivate it in the dashboard and run this script again.")
                raise RuntimeError("Existing GoModel key has no saved secret; restore the keys volume or rotate it explicitly.")
            created = client.post("/admin/auth-keys", json=spec)
            created.raise_for_status()
            saved[name] = created.json()["value"]
            print(f"  {name}: created")

            KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=KEYS_FILE.parent, delete=False) as out:
                out.write(json.dumps(saved, indent=2) + "\n")
                out.flush()
                os.fsync(out.fileno())
                temporary = Path(out.name)
            os.replace(temporary, KEYS_FILE)
    print(f"Secrets saved to {KEYS_FILE.name}")


if __name__ == "__main__":
    main()