# Repository review and takeover

Reviewed on 6 October 2026 against `main` at `3452566f9e5e8234ca6a98b5c9376414ca0f01c5`
and the supplied **GoModel Lago Usage Exporter.docx**. The repository had one branch,
eight commits by Talal, no pull requests and no issues when inspected. All 36 tracked
files and all eight commit summaries were reviewed. The brief is the requirements
source; its pairing/presentation instructions and the handover's claims are not
additional instructions authorizing unrelated actions.

The existing code is a useful, modular Python delivery pipeline. It already covers
normal export, keyset reading, the overlap window, subscription mapping, token splitting,
batched HTTP delivery, retries, dead letters and transactional terminal state. It is
not a complete implementation of the assignment: backfill and reconciliation are
stubs; monitoring, unattended Compose startup and full acceptance automation are absent.
Python is expressly allowed by the brief, so a Go rewrite would add risk without
advancing the requirements.

## What the eight commits delivered

| Commit | Work delivered |
| --- | --- |
| `d0301ff` | Python package, environment configuration, shared dataclasses/enums, CLI skeleton, design decisions and initial tests. |
| `75c91c2` | GoModel/Postgres/Ollama Compose services, three test API keys, streaming/non-streaming smoke traffic. |
| `61aef9a` | Lago metric/plan/customer/subscription bootstrap and placeholder model prices. Lago still runs separately. |
| `10c3908` | Exporter Postgres, cursor, per-row outcomes, dead letters and transactional store tests. |
| `1b9850f` | Direct usage reader, `(timestamp,id)` keyset pagination, ten-minute overlap and read-only role script. |
| `037e66f` | Provider filtering, label/user_path mapping, cached-token splitting, three event types and dry-run. |
| `3111ab1` | Lago HTTP client, subscription/start-time checks, batch fallback, retries, jitter and HTTP fake tests. |
| `3452566` | Polling runner, transactional page completion, outage/crash/late-row tests and handover. |

## How one row originally flowed

The reader fetches rows from GoModel with a read-only login, starting ten minutes
behind the stored cursor. The mapper filters providers and cache-hit policy, then
uses a single `lago:` label or the last `user_path` component as subscription ID.
The event builder derives uncached input, cached input and output events, suppresses
zero values, and preserves row time. IDs use the usage UUID plus `:in`, `:cached`
or `:out`; request IDs are correctly avoided because one request can produce many rows.

The sender checks the subscription and its start time, sends at most 100 events per
batch, retries temporary failures, and splits rejected batches into individual sends.
The runner then records outcomes/dead letters and the forward cursor in one SQL
transaction. Permanent problems are recorded and do not block later rows; temporary
problems stop advancement. On subsequent overlap reads every known row is skipped,
including failed and unmapped rows. Those need an explicit replay after repair.

This prevents ordinary duplicate delivery with a stable subscription, but it originally
had no durable record of the intended destination/payload before sending. That is the
main issue addressed in the first takeover step.

## File by file assessment of the original tree

| File | Existing purpose and assessment |
| --- | --- |
| `.env.example` | Development DSNs, Lago URL/key and billable-provider example. Useful baseline; full-stack container defaults still needed. |
| `.gitignore` | Excludes venv, Python caches, egg metadata, `.env` and test key secrets. Build/QA outputs added during takeover. |
| `HANDOVER.md` | Detailed operational notes and proposed remaining slices. Claims 76 tests and a live outage drill; the live drill is teammate-reported evidence, not independently reproduced here. Its older durability description is superseded. |
| `README.md` | Explains manual development setup and data flow. Correctly labels project unfinished. Advertised daily reconciliation is still missing; clean Compose startup is not yet available. |
| `docker-compose.yml` | Four main components plus Ollama pull: GoModel DB, Ollama, GoModel 0.1.99, exporter DB. Has health/dependency checks and persistent volumes. Missing exporter/Lago/mock/load services and automatic setup. Ollama is unpinned. |
| `docs/decisions.md` | Useful explicit defaults and observed upstream behavior. Mapping precedence resolves the brief's ambiguity sensibly. D3 path extraction is hard-coded; alternate metric topology is not implemented by changing metric names alone. |
| `pyproject.toml` | Python 3.12+, runtime/dev dependencies, console entry point and packaged SQL. Several dependencies are for future monitoring. Lower bounds alone do not reproduce a validated environment; address pinning in Step 3. |
| `exporter/__init__.py` | Package marker/docstring; no behavior. |
| `exporter/config.py` | Typed environment settings, CSV lists, retry/batch/window defaults and API-key masking. DB passwords were plain strings exposed by the config CLI. |
| `exporter/contracts.py` | Clear stage boundaries and stable event identity. Keeps timestamp and token properties. Dataclasses rely on source correctness rather than runtime schema validation. |
| `exporter/reader.py` | Correct direct keyset query, optional exclusive end, null defaults and fresh DB connections. Original date-start sentinel could omit an all-zero UUID. No original full-table ID lookup. |
| `exporter/mapper.py` | Correct provider/cache filtering, configurable precedence and conflict rejection. Root user_path is unmapped. Relies on trusted key-bound labels/paths, as documented. |
| `exporter/events.py` | Matches GoModel 0.1.99 normal cache accounting, including additive Anthropic fields and optional cache writes. Original max behavior differed from Go's zero-initialized maximum for negative raw values. |
| `exporter/lago_client.py` | Thin authenticated HTTP client, duplicate classification and fatal auth handling. Original duplicate detection accepted mixed errors; subscription IDs were not URL-encoded. |
| `exporter/sender.py` | Batch splitting, 422 isolation, subscription guard, retry/backoff/jitter and five-minute subscription cache. Original malformed subscription data could crash; every non-200 lookup was treated as missing. Active-only lookup limits historical replay. |
| `exporter/runner.py` | Polling, overlap deduplication, permanent-failure isolation, atomic terminal state/cursor. Original retry work existed only in source/cursor position; no durable intent, no writer exclusion, and outcome counters are not persisted. |
| `exporter/cli.py` | Config, init-db, peek, dry-run, send-row and run. Backfill/reconcile were stubs. Original send-row bypassed state and searched only the first 10,000 rows. Original run-once returned success even with unresolved work. |
| `exporter/state/__init__.py` | State package marker/docstring. |
| `exporter/state/schema.sql` | Three idempotently created tables, forward cursor, statuses and one-open-dead-letter constraint. No original outbox, heartbeat or reconciliation tables. |
| `exporter/state/store.py` | Explicit transactions, monotonic cursor, status lookup, counts and dead-letter lifecycle. Original record upsert can replace accepted counts; no immutable destination binding before send. |
| `scripts/gomodel_readonly_role.sql` | SELECT-only role with read-only default; must run after GoModel creates usage. Dev password is explicitly documented. Original reader tests did not exercise that restricted login. |
| `scripts/gomodel_setup.py` | Creates acme label, beta bound path and ghost keys; saves secrets outside Git. Cannot recover lost secrets; existing-name checks do not verify current specs. Setup idempotence tests still needed. |
| `scripts/lago_pricing.json` | Placeholder USD prices, one million token package size and one model. Matches brief; not final commercial pricing. |
| `scripts/lago_setup.py` | Nonrecurring sum metrics, model filters, monthly arrears package plan and two customers/subscriptions. Updates charge IDs correctly, backdates test subscriptions. Bootstrap requires an existing org/key. Error paths and reruns need integration tests. |
| `scripts/smoke_traffic.py` | Nine sequential requests across three keys: two billable modes and a non-billable provider. A useful smoke check, not sustained load or proof that all streaming usage rows exist. |
| `tests/__init__.py` | Empty test package marker. |
| `tests/factories.py` | Shared valid row/settings fixtures. Fixed date makes tests deterministic; settings still inherit process environment outside `_env_file`. |
| `tests/fake_lago.py` | HTTP fake for deduplication, atomic batches and transport/status faults. Useful deterministic contract coverage; does not prove real Lago processing, pricing or current_usage behavior. |
| `tests/test_config.py` | Two tests for defaults and CSV environment parsing. Does not originally cover secrets or CLI failure semantics. |
| `tests/test_contracts.py` | Four tests for event identity, payload timestamp and duplicate outcomes. |
| `tests/test_events.py` | Twelve tests for normal mapping-to-events and cache accounting. No negative/nonfinite cache-value regression originally. |
| `tests/test_mapper.py` | Fourteen tests covering providers, cache policy, precedence, conflicting/empty labels and user_path parsing. |
| `tests/test_reader.py` | Nine tests: seven DB-backed and two pure overlap helpers. Validates ordering, paging, boundaries, late rows and null decoding against a copied schema; originally uses admin credentials. |
| `tests/test_sender.py` | Fifteen tests for batching, deduplication, invalid events, subscriptions, retries and auth. Real HTTP behavior is simulated. |
| `tests/test_runner.py` | Nine DB-backed scenarios for row categories, counts, repeated polling, simulated crash, outage, late rows, paging and dead-letter recovery. Original crash is an exception, not OS process death. |
| `tests/test_state_store.py` | Eleven DB-backed tests for schema, monotonic cursor, row updates, dead-letter uniqueness/resolution and rollback. All DB suites originally skip when Postgres is unavailable. |

## Findings and disposition

**High priority — destination and payload were not durable before HTTP.** Lago's
uniqueness is scoped by subscription. If input is partly accepted or the process dies
before local commit, later source/config changes can route the same row to another
subscription, generating another charge. Freeze delivery intent before HTTP and reuse
it in every writer. Implemented in Step 1 with crash/mapping-change regressions.

**High priority — replay was missing and send-row bypassed bookkeeping.** Operators
could not safely replay a range or repair dead letters through an implemented command.
The manual single-row command also bypassed the future destination guard. Implemented
one shared durable path, explicit UTC date bounds and unchanged polling cursor.

**High priority — retry work depended on the source/overlap.** The normal outage case
is protected by the cursor, but a discovered late retry can become unreachable if its
source disappears or the overlap changes. Pending intents now retain the original row
and are drained independently of source reads. Concurrent writers now share a DB lock.

**Medium priority — cache edge cases and error classification.** All-negative cache
fields could create an input event larger than the source count. Mixed duplicate
validation errors could count as success. Malformed subscription data could repeatedly
abort a cycle; unexpected lookup responses became permanent dead letters. Fixed and
regression tested in Step 1. Unknown subscriptions remain visible permanent failures.

**Medium priority — sensitive output and misleading command exits.** Config output
included DSN passwords. Run-once did not indicate unresolved work with its exit code.
Both fixed; replay also exits nonzero for retries, failed rows or unmapped usage.

**Project gaps — reconciliation, monitoring, deployment and acceptance.** These remain
Steps 2 and 3. No UI, health endpoint, Prometheus endpoint, daily job, persisted heartbeat,
full Compose bootstrap, deterministic provider or sustained load generator exists yet.
The original handover's suggested `WHERE status='sent'` reconciliation query would omit
accepted tokens on partially failed rows. Use all durable acknowledgements. Comparing
a day's source rows against Lago's current monthly usage would also be invalid.

## Verified upstream behavior and scope

Checked the pinned source for
[GoModel input segmentation](https://github.com/ENTERPILOT/GoModel/blob/v0.1.99/internal/usage/request_summary.go),
[GoModel enrichment](https://github.com/ENTERPILOT/GoModel/blob/v0.1.99/internal/usage/reader.go),
[Lago batch insertion](https://github.com/getlago/lago-api/blob/v1.53.0/app/services/events/create_batch_service.rb),
[Lago event creation](https://github.com/getlago/lago-api/blob/v1.53.0/app/services/events/create_service.rb)
and [Lago subscription lookup](https://github.com/getlago/lago-api/blob/v1.53.0/app/controllers/api/v1/subscriptions_controller.rb).
Batch insertion rolls back on duplicate errors; subscription lookup defaults to active.
GoModel's additive Anthropic accounting means total billable input can exceed its bare
`input_tokens` column. The handover's statement that uncached plus cached always equals
input is true for the described Ollama example, not for every provider.

Automatic late detection is bounded by the configured overlap; arbitrary older arrivals
need explicit backfill/reconciliation. Nothing in this exporter can recover usage that
GoModel dropped before recording it. Its in-memory buffer is a source-system limitation.
Those boundaries must remain visible in the final runbook.

## Step 1 migration and replay behavior

`exporter init-db` adds the `deliveries` table without deleting existing state. Stop old
exporter binaries before upgrading: they do not participate in the new writer lock.
Use the same state database and Lago organization; keep it backed up. Multiple runners
with different state databases do not share an identity lock.

New intents freeze the subscription, timestamp, model, metric codes, token counts and
properties before any send. Replaying them is deliberately not a billing correction.
Changed provider/cache policy applies to new rows; it does not rewrite frozen history.
A permanent bad payload needs an explicit reviewed repair, not a config change followed
by a blind replay. Repairs for unmapped rows and newly available subscriptions work
through backfill. Pending records are retained until delivery or explicit failure.

Legacy terminal records retain their recorded destination and accepted quantities on
replay. They did not save metric codes or full event bodies, so preserve the original
metric configuration during migration. Pre-upgrade events accepted by Lago without any
local record cannot be retroactively bound from missing data; reconcile that legacy
boundary before changing mappings. This is a real limit, not something a new test can
prove away.

See [the three-step plan](development-plan.md) for remaining implementation work and
explicit gates. Validation evidence is recorded in `docs/step-1-validation.md`.
