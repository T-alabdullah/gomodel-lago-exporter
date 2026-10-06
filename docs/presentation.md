# 18-minute presentation and live demonstration

Prepare the complete demo before presenting. Use a fresh isolated project for the
acceptance harness, keep `.env.demo` private, and check all required images are local.
Open the status page and optionally the Lago frontend. Both teammates should be able
to explain the durable intent, row identity, cache split and reconciliation scopes.

| Minutes | Demonstration and explanation |
| --- | --- |
| 0–3 | Show the README architecture. Requests go through stock GoModel; the exporter reads recorded PostgreSQL usage separately and has no role in live request latency. Explain three token metrics and label/user_path mapping. |
| 3–6 | Run mixed traffic; show streaming/plain responses across two models. In source/state and Lago, follow one usage UUID through `:in`, `:cached`, `:out`. Explain 120 prompt tokens split into 90 uncached + 30 cached, with 12 output tokens. Show excluded provider rows. |
| 6–8 | Show ghost's unmapped row on the status page and in dead letters. Explain why it is visible and why readiness is 503. Show the documented repair/backfill procedure. |
| 8–11 | Stop `lago-api`, generate mapped traffic, and wait at least ten seconds. Show pending state/lag. Restart Lago and show catch-up with the original timestamps and IDs. |
| 11–14 | Run the automated acceptance scenarios or show their saved evidence: accepted-before-ack SIGKILL, repeated backfill with unchanged counts, inside-overlap late row and older explicit replay. Explain why retry does not mean a second charge. |
| 14–16 | Show the deliberately removed event in the reconciliation report with its source row ID. Restore/replay it and show zero differences. Distinguish exact-range event evidence from whole billing-period billed-unit evidence. |
| 16–18 | Show CI, status/metrics, retained-volume restart and the runbook. Explain limits: unrecorded GoModel drops, source retention, immutable correction/closed invoices, package rounding and one writer. |

Useful commands (from repo root):

```bash
docker compose --env-file .env.demo run --rm traffic
# For an outage demo that avoids adding new unmapped rows:
docker compose --env-file .env.demo stop lago-api
docker compose --env-file .env.demo run --rm traffic python -c "from demo.traffic import run; run(10, customers=('acme','beta'))"
# Keep the outage at least ten seconds, inspect the status page, then:
docker compose --env-file .env.demo start lago-api
```

For the full automated sequence, use `python3 demo/acceptance.py` immediately after
starting an empty stack, before manually generating traffic. It writes ordered,
assertion-backed evidence to `acceptance-results.json`; do not claim a failed scenario
passed. CI artifacts are a reproducible backup if presentation networking fails.

Navigator questions: Where is intent committed relative to HTTP? What if only Lago
committed? What if mapping changes during a retry? Why isn't today's Lago monthly
usage proof of yesterday? What remains unknown after source retention? Why can a
billing readiness failure coexist with a healthy process?
