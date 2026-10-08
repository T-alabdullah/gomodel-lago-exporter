# GoModel → Lago Usage Exporter

A standalone Python service that reads recorded GoModel usage through a read-only
PostgreSQL login and reports token events to Lago. Immutable delivery records,
subscription-scoped event IDs and replay protect billing across retries and process
restarts. Daily reconciliation checks source expectations, exporter acknowledgements,
Lago events and independently scoped Lago billing-period usage.

## Start the complete demo

Requirements: Docker Engine/Desktop with Compose v2 supporting `--wait`, Python 3,
OpenSSL, and enough resources for GoModel, Lago's Rails services and three databases.
The acceptance environment is Linux amd64. Allow several minutes for first image
pulls and Lago migrations. Images use explicit release tags; upgrade them deliberately.

```bash
git clone https://github.com/T-alabdullah/gomodel-lago-exporter.git
cd gomodel-lago-exporter
# While the takeover PRs are unmerged:
git switch codex/complete-acceptance
python3 scripts/init_demo.py
docker compose --env-file .env.demo up -d --build --wait --wait-timeout 600
docker compose --env-file .env.demo run --rm traffic
```

`init_demo.py` creates private, random demo credentials once. Repeating it retains
credentials so existing database volumes remain usable. Keep `.env.demo` out of Git.
Compose provisions Lago's organization, metrics, two-model package plan, customers,
subscriptions, GoModel keys, and the exporter's read-only source role automatically.
It validates the actual upstream usage schema after GoModel migrates it.

Open [exporter status](http://localhost:8000), [GoModel](http://localhost:8080), or
[Lago API health](http://localhost:3000/health). Databases and the mock model provider
are internal to the Compose network; HTTP ports bind only to localhost.

The bounded traffic command sends 240 successful requests by default: streaming and
plain responses, two deterministic models, acme/beta/ghost keys, billable `ollama-qai`
and excluded `ollama-ext`. No model weights or external inference API are needed.
`ghost` deliberately has no customer mapping, so dead letters and `/health` 503 are
expected after this traffic. Its presence demonstrates visibility, not a lost row.

For sustained mixed traffic:

```bash
docker compose --env-file .env.demo run --rm traffic python -m demo.traffic --rounds 100 --concurrency 6 --pause 0.25
```

Optional Lago UI:

```bash
docker compose --env-file .env.demo --profile ui up -d lago-front
```

Open [Lago](http://localhost:8081), sign in as `demo@example.test` using
`LAGO_ORG_USER_PASSWORD` from your private `.env.demo`. Placeholder prices are in
`scripts/lago_pricing.json`: three nonrecurring sum metrics, model filters and package
charges per 1,000,000 tokens. Package pricing rounds to whole packages; these are
assignment placeholders, not a claim of proportional per-token currency billing.

## Architecture and guarantees

```mermaid
flowchart LR
    T[Mixed demo traffic] --> G[Stock GoModel]
    G --> M[Deterministic model provider]
    G --> U[(GoModel usage)]
    U -->|SELECT only| E[Exporter reader and mapper]
    E --> S[(Durable intent, acknowledgements, cursor)]
    S --> D[Batch sender and retries]
    D --> L[Lago API and worker]
    L --> B[(Lago events and billing)]
    U --> R[Reconciliation]
    S --> R
    L --> R
    R --> H[Status, health, Prometheus]
```

- Each usage UUID yields up to three events (`:in`, `:cached`, `:out`). Original
  timestamps and subscription IDs remain fixed across replay. Input-cache splitting
  follows the pinned GoModel semantics.
- A `lago:<subscription>` label wins; otherwise mapping uses the last `user_path`
  segment. Only configured provider names are billed. Unknown subscriptions and
  unmapped billable rows remain visible in dead letters.
- Exact payloads commit before HTTP. If the process dies after Lago accepts a batch,
  restart uses the same IDs; duplicate responses count as acknowledged. Durable
  pending work is independent of the polling overlap and survives source deletion.
- The cursor advances with recorded outcomes. Temporary delivery failures stop
  forward progress; permanent failures remain visible for repair. Backfill does not
  move the polling cursor.
- A shared PostgreSQL advisory lock serializes delivery, replay and reconciliation.
  Reconciliation compares exact-range events separately from whole billing periods;
  incomplete evidence is never reported as zero difference.

The exporter cannot recover requests GoModel never records. The demo increases the
usage buffer to 10,000 and checks successful traffic against recorded row counts.
In deployment, collect GoModel logs and alert on usage-buffer drop warnings. Overlap
catches bounded late rows; older missing rows require explicit backfill. Source
retention limits provable reconciliation history.

## Operate and recover

```bash
# View service logs and status.
docker compose --env-file .env.demo logs --tail 100 exporter
curl http://localhost:8000/status
curl http://localhost:8000/metrics

# Run an audit or replay with the polling writer stopped.
docker compose --env-file .env.demo stop exporter
docker compose --env-file .env.demo run --rm --no-deps exporter exporter reconcile --start 2026-10-01 --end 2026-10-06
docker compose --env-file .env.demo run --rm --no-deps exporter exporter backfill 2026-10-01 2026-10-06
docker compose --env-file .env.demo start exporter

# Re-run setup deliberately; existing keys/charges/subscriptions are retained.
docker compose --env-file .env.demo run --rm --no-deps bootstrap

# Stop while preserving usage, state, Lago data and credentials.
docker compose --env-file .env.demo down
```

Use dates containing your traffic; ranges are UTC, start included/end excluded.
`reconcile` exits 0 only for a matched report. `/health` is billing readiness, so a
missing audit, stale worker, backlog or unresolved failure can yield 503. Container
liveness uses `/status` so billing alerts do not cause restart loops. The demo uses
a 2-second grace; production defaults remain 15 minutes.

**Do not use `down -v` to recover a deployment:** it deletes billing state and data.
Back up `.env.demo` with all database volumes and the `demo-secrets` volume. The
source credentials supplied to the exporter have SELECT-only access; admin credentials
are confined to the explicit demo provisioning/acceptance tools.

See [operations and repair](docs/operations.md), [deployment and failure runbook](docs/deployment.md),
and the [15–20 minute demo outline](docs/presentation.md). Prepaid enforcement is out
of scope. Use an authenticated reverse proxy and managed secrets before exposing
this local demo as a production service.

## Tests and acceptance

**Completed:** 201 regression tests (zero skips) and all 11 real GoModel/Lago
acceptance scenarios passed. See [recorded results and limits](docs/step-3-validation.md).

Unit and PostgreSQL regression tests:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps --no-build-isolation -e .
EXPORTER_TEST_ADMIN_DB_URL=postgresql://postgres:exporter@localhost:5434/postgres python -m pytest -q --require-db
```

Use a **dedicated test PostgreSQL server**, not the application databases. Tests create
and reset test databases. Missing database tests may skip without `--require-db`;
skips do not pass the gate. CI verifies the original 76-test handover baseline
independently, runs the expanded suite without skips, and checks packaging/dependencies.

Real-stack acceptance, only on a fresh isolated demo with no usage:

```bash
python3 scripts/init_demo.py
docker compose --env-file .env.demo up -d --build --wait --wait-timeout 600
python3 demo/acceptance.py
```

The harness deliberately stops services, kills an exporter after a real successful
Lago response, inserts late source fixtures, repairs ghost mappings and deletes/restores
one Lago event. It refuses a source database already containing usage. It writes
`acceptance-results.json` and preserves the stack for inspection. The independent
CI acceptance job boots from empty volumes and uploads evidence and logs.

[Development plan](docs/development-plan.md) · [original repository review](docs/repository-review.md) ·
[Step 1 validation](docs/step-1-validation.md) · [Step 2 design](docs/step-2-design.md).
`HANDOVER.md` records the teammate's original implementation and commands; this README
and the current runbooks supersede its setup instructions.
# Interactive local Llama lab

For a browser demo with real Llama inference, request tracing, database inspection,
Lago evidence, replay/failure controls and reconciliation, run
`python3 scripts/run_live.py` and open `http://localhost:8090`.
See [the lab guide](docs/live-lab.md) for the guided experiment and runtime details.
For a fresh Windows machine, follow the [copy-paste Windows setup guide](docs/live-lab-windows.md).
