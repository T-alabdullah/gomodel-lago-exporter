# Deployment and failure runbook

## Components and trust boundaries

The default Compose stack runs stock GoModel 0.1.99, Lago API/worker/clock v1.53.0,
Lago's PostgreSQL/Redis, a separate GoModel PostgreSQL, a separate exporter PostgreSQL,
the exporter, and a deterministic OpenAI-compatible mock. `bootstrap` is a one-shot
admin job; `traffic` is an on-demand tool profile. Lago's frontend is optional.

Lago's migration task uses its supported organization-seeding environment variables.
The bootstrap waits for both real APIs and the Lago worker, checks GoModel's migrated
column types, creates a SELECT-only role, verifies even an explicitly writable
transaction cannot DELETE, then provisions test keys, metrics, pricing and subscriptions.
The exporter receives neither source-admin nor Lago-database credentials.

This is a local demonstration topology. The mock is deliberately not a model server.
For actual vLLM/Ollama deployment, replace provider URLs, model IDs and placeholder
prices, retain pinned releases, provision real customer mappings and use production
credentials. Ordinary traffic must never call the acceptance mutation helpers.

## Fresh start and retained restart

Follow the README's exact sequence: generate `.env.demo`, then Compose up with that
env file. Default Compose startup requires no manual signup, API-key copying, role
SQL or model download. Traffic is on demand to avoid unexpected ongoing work.

Re-running the generator preserves secrets; re-running bootstrap reuses saved GoModel
key values and updates existing Lago metric/charge identities. Missing key secrets
cause a visible failure: recover the secrets volume or deliberately rotate the key.
Do not create duplicate keys and pretend setup succeeded.

`docker compose --env-file .env.demo down` retains volumes. A following `up -d --wait`
runs the migration/setup dependency chain and resumes the original state. Preserve
all three PostgreSQL volumes, Redis/storage, demo key secrets and `.env.demo` together.
For production use consistent database backups with tested restores and managed
secret storage. The demo generator is not a secret-rotation system.

Upgrades: stop old exporter processes; back up state; test new pins in an isolated
stack; initialize additive state schema; then start one exporter. Do not change
stored event IDs or frozen destinations. An upstream schema/API change must fail
tests before reaching the running exporter.

## Failure handling

| Symptom | Evidence and action |
| --- | --- |
| Lago down/rate-limited/5xx | Pending durable deliveries and lag grow; retries back off. Restore Lago and let polling catch up. Audit the affected dates after grace. |
| Exporter killed after HTTP | Saved intents may exist without local acknowledgement. Restart; unchanged event IDs turn Lago duplicates into acknowledgements. |
| Source DB unavailable | The next read reconnects; status reports unavailable/stale evidence. Restore the source, then verify the affected range. |
| Unknown subscription or no mapping | Inspect dead-letter row IDs. Repair the missing subscription or an as-yet-unbilled source mapping, stop polling, backfill, audit and resume. |
| Late insertion within ten minutes of cursor | Normal overlap sends it. Already-sent IDs remain stable. |
| Older insertion outside overlap | Audit identifies missing acknowledgement/event; explicitly backfill its original range. |
| Missing Lago event after recorded success | Audit lists its row ID. Normal force-backfill resends the frozen payload with the original identity; verify current-period billing afterwards. |
| Altered payload/customer or closed invoice | Ordinary replay cannot rewrite immutable identity or recalculate finalized invoices. Investigate and perform an explicit billing correction; see operations.md. |
| `/health` 503 but process is running | Read JSON alert reasons. Ghost dead letters, missing/incomplete audits or a stale heartbeat are legitimate readiness failures. Do not reset state to clear the alert. |
| GoModel buffer drops | The exporter cannot reconstruct unrecorded usage. Alert on GoModel's buffer/drop warnings, reduce load/increase capacity and investigate missing request evidence. |
| Initial boot fails | `docker compose --env-file .env.demo ps -a` and logs for `lago-migrate`, `gomodel`, `bootstrap`. Preserve volumes while diagnosing. Never print the full expanded Compose configuration into shared logs: it contains secrets. |

The operation commands and evidence boundaries are detailed in [operations.md](operations.md).
The exporter uses original event timestamps; it does not reinterpret old usage as
current usage to get it accepted by Lago.

## Alerts and scaling

Scrape `/metrics` from the exporter. Route `exporter_alert` reasons, open dead letters,
backlog age above 900 seconds, stale workers and unresolved reconciliation periods
into the team's monitoring system. No external notification destination is configured
in the local demo. Also monitor GoModel usage-buffer warnings and Lago worker errors.

Status scans are capped and raise an incomplete-scan alert rather than implying the
queue is empty. Reconciliation pages Lago and rejects unstable pagination; large
billing periods may hold the shared writer lock for a while. One writer is deliberate
for this task. Measure throughput and audit duration before changing the architecture.
Exact token equality does not validate currency amounts, taxes or rounded packages.

Python dependencies are fixed in `requirements.lock` for the container and CI.
Application/database image tags are explicit. Tags can be republished by upstream;
record and review resolved image digests from acceptance evidence for a release.
Linux amd64 is the tested target; other architectures must pass the same gate.
