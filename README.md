# GoModel → Lago Usage Exporter

Reads GoModel's recorded usage and sends it to Lago as billable events, with no
double billing, no lost usage after an outage, and a daily reconciliation.

> Work in progress. See `docs/decisions.md` for every behaviour choice.

> **Takeover plan:** [three major steps](docs/development-plan.md), [full repository review](docs/repository-review.md), and [Step 1 validation](docs/step-1-validation.md).

> **Original handover:** Start with [HANDOVER.md](HANDOVER.md): how to run it, how to check it works, and where Steps 9–14 plug in.

## Quick start (development)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
exporter config
```

## Running the dev stack

**GoModel** (defined in this repo's `docker-compose.yml`):

```bash
docker compose up -d
exporter init-db                   # creates the exporter's own tables (cursor, rows, dead letters)
docker compose exec -T gomodel-db psql -U postgres -d gomodel < scripts/gomodel_readonly_role.sql   # read-only login for the exporter
python scripts/gomodel_setup.py    # creates the test API keys (acme, beta, ghost)
python scripts/smoke_traffic.py    # sends sample traffic -> usage rows
exporter peek                      # shows the usage rows the exporter can read
exporter dry-run                   # shows what WOULD be sent to Lago (sends nothing)
exporter send-row <id>             # sends ONE row's events to Lago (testing; safe to repeat)
exporter run                       # the exporter itself: every 10 s, Ctrl+C to stop
exporter run --once                # one cycle, then exit
```

**Lago** (cloned next to this repo, pinned to v1.53.0):

```bash
cd ..
git clone https://github.com/getlago/lago.git
cd lago
git checkout v1.53.0
echo "LAGO_RSA_PRIVATE_KEY=\"$(openssl genrsa 2048 | openssl base64 -A)\"" >> .env
docker compose up -d
```

Then open http://localhost, sign up, copy the API key from
**Developers → API keys** (not the Organization ID) into `EXPORTER_LAGO_API_KEY`
in this repo's `.env`, and run:

```bash
python scripts/lago_setup.py       # metrics, plan, test customers (safe to re-run)
```

Prices are placeholders in `scripts/lago_pricing.json`. Edit and re-run to change them.
Step 12 will fold Lago into this repo's `docker compose up`.

## How data flows

```
reader  ->  UsageRow  ->  mapper  ->  MappingResult  ->  events  ->  LagoEvent  ->  sender  ->  SendResult
```

The types are defined in `exporter/contracts.py`. Each stage only talks to the
next through them.

## Build progress

| Step | Part | Status |
|---|---|---|
| 1 | Repo skeleton, config, contracts | ✅ |
| 2 | GoModel environment | ✅ |
| 3 | Lago environment + bootstrap | ✅ |
| 4 | State DB | ✅ |
| 5 | Reader | ✅ |
| 6 | Mapper + event builder | ✅ |
| 7 | Lago client + sender | ✅ |
| 8 | Runner + HANDOVER.md | ✅ |
| 9 | Backfill | ✅ takeover Step 1 |
| 10 | Reconciliation | ✅ takeover Step 2 |
| 11 | Status page, metrics, health | ✅ takeover Step 2 |
| 12 | Full test environment | ⬜ |
| 13 | Failure tests | ⬜ |
| 14 | README, runbook, presentation | ⬜ |

## Safe replay and recovery

```bash
exporter backfill 2026-10-01 2026-10-06
exporter backfill 2026-10-05T00:00:00Z 2026-10-05T12:00:00Z
```

The start is included and the end is excluded. Dates mean midnight UTC; timestamps
must include a timezone. Re-run the same range after interruption. Replay does not
advance the normal polling cursor. Fix an unmapped source row or create its missing
subscription, then replay its range to close the dead letter.

Every mapped row now saves its destination and exact event payload in `deliveries`
before HTTP. Polling, backfill and `send-row` share this durable path. Pending records
are retried even if source rows disappear; payloads and destinations remain frozen.
One state-database writer runs at a time. If a backfill finds an active polling cycle,
retry when that cycle ends or stop the polling process first. Run-once and replay exit
nonzero when work is retrying, rejected or unmapped.

Run `exporter init-db` on upgrade, after stopping old exporter versions. Preserve the
state database and original metric configuration for legacy rows. See the review for
legacy-data limits and the difference between safe replay and correcting a charge.

## Test gate

```bash
python -m pytest -q --require-db
```

Set `EXPORTER_TEST_ADMIN_DB_URL` to a **dedicated test PostgreSQL server** (default:
`postgresql://postgres:exporter@localhost:5434/postgres`). The tests create named test
databases and reset their tables. Do not point them at production. Without `--require-db`,
local unit tests can run while unavailable database tests skip; that is not a completed
major-step gate. GitHub Actions runs the original baseline and expanded suite on
PostgreSQL 16, forbids skips, checks dependencies and builds the distributable package.


## Reconciliation and status

```bash
exporter init-db
exporter serve                         # delivery + daily reconciliation + web status
exporter reconcile                     # audit yesterday UTC on demand
exporter reconcile --start 2026-10-05 --end 2026-10-06
```

Open http://127.0.0.1:8000 for the status page. `/status` returns JSON, `/metrics`
exposes Prometheus metrics and `/health` returns readiness with alert reasons.
`exporter run` remains delivery-only. A reconcile command exits 0 only for a matched
report; a mismatch or incomplete result exits 1 and is saved for investigation.

The audit compares exact-range events, then separately checks Lago's returned billing
periods. It includes acknowledgements on partially failed rows and never equates a
missing API response to zero usage. See [operations and repair](docs/operations.md)
for scheduling, configuration, supported scopes, alerts and safe replay, and
[Step 2 design and tests](docs/step-2-design.md) for the upstream contracts and evidence.
