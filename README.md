# GoModel → Lago Usage Exporter

Reads GoModel's recorded usage and sends it to Lago as billable events, with no
double billing, no lost usage after an outage, and a daily reconciliation.

> Work in progress. See `docs/decisions.md` for every behaviour choice.

## Quick start (development)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
pytest
exporter config
```

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
| 2 | GoModel environment | ⬜ |
| 3 | Lago environment + bootstrap | ⬜ |
| 4 | State DB | ⬜ |
| 5 | Reader | ⬜ |
| 6 | Mapper + event builder | ⬜ |
| 7 | Lago client + sender | ⬜ |
| 8 | Runner + HANDOVER.md | ⬜ |
| 9 | Backfill | ⬜ |
| 10 | Reconciliation | ⬜ |
| 11 | Status page, metrics, health | ⬜ |
| 12 | Full test environment | ⬜ |
| 13 | Failure tests | ⬜ |
| 14 | README, runbook, presentation | ⬜ |