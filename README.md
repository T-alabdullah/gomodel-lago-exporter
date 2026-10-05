# GoModel → Lago Usage Exporter

Reads GoModel's recorded usage and sends it to Lago as billable events, with no
double billing, no lost usage after an outage, and a daily reconciliation.

> Work in progress. See `docs/decisions.md` for every behaviour choice.

> **Picking this up?** Start with [HANDOVER.md](HANDOVER.md): how to run it, how to check it works, and where Steps 9–14 plug in.

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
| 9 | Backfill | ⬜ |
| 10 | Reconciliation | ⬜ |
| 11 | Status page, metrics, health | ⬜ |
| 12 | Full test environment | ⬜ |
| 13 | Failure tests | ⬜ |
| 14 | README, runbook, presentation | ⬜ |