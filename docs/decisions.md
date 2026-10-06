# Decisions

Every behaviour choice lives here, with the config switch that controls it.
Changing a decision means changing config, not code.

| # | Decision | Default | Switch | Source |
|---|---|---|---|---|
| D1 | Are response-cache hits billed? | Yes | `EXPORTER_BILL_CACHE_HITS` | Task doc default |
| D2 | How is a row matched to a Lago subscription? | `lago:` key label first, then `user_path` | `EXPORTER_MAPPING_ORDER` | Task doc ("What to build") |
| D3 | How does `user_path` become a subscription id? | Its last segment (`/customers/sub_acme` → `sub_acme`) | (code, Step 6) | Our choice |
| D4 | One metric per model, or one metric with model as a property? | One metric per token type, `model` as a property | `EXPORTER_METRIC_*` | Task doc default |
| D5 | How long can billing lag before it alerts? | 15 minutes | `EXPORTER_LAG_ALERT_SECONDS` | Task doc default |
| D6 | How are prompt-cache *write* tokens billed? | As uncached input | `EXPORTER_CACHE_WRITE_BILLING` | Our choice (see below) |

## Fixed rules (not configurable, on purpose)

- **transaction_id = usage row id + `:in` / `:cached` / `:out`.** Never `request_id`: one request can produce several usage rows. Lago deduplicates on this id, which is what makes retries and backfills safe. Changing the format would double-bill everything already sent.
- **Read GoModel's `usage` table directly**, ordered by `(timestamp, id)`. Not the `/admin/usage/log` API: it pages by offset, newest first, so new rows shift the pages.
- **Send token counts, not cost.** GoModel's cost fields are estimates; Lago does the pricing.
- **Always send `timestamp`** (the usage row's time). Otherwise Lago uses arrival time, which is wrong for late or backfilled rows.

## Notes

**D2:** the task doc's decisions list says only "user_path", while "What to build" says label first with user_path as fallback. We followed "What to build" and made the order configurable.

**D6:** GoModel splits input tokens into three parts (`EntryInputSegments` in `internal/usage/request_summary.go`): uncached, cached reads, and cache writes. The task doc only defines metrics for the first two. Providers charge cache writes as input (often at a premium), so we bill them as uncached input rather than silently dropping them. Ollama/vLLM don't produce cache-write tokens, so this only matters for providers like Anthropic.

## Out of scope

Enforcing prepaid balances. GoModel's budgets handle limits at request time; this exporter only reports usage.


## Known risks

**R1: clients can set `user_path` on keys that don't have one bound.** GoModel takes `user_path` from the `X-GoModel-User-Path` request header when the API key has no bound user path. A key with neither a `lago:` label nor a bound user path could send `/customers/sub_beta` and be billed to beta (verified on GoModel 0.1.99). Keys with a bound user path ignore the header, and labelled keys are safe because the label is checked first. **Rule: every key for billable traffic must have a `lago:` label or a bound `user_path`.**

**R2: GoModel deletes usage rows after 90 days** (`USAGE_RETENTION_DAYS`, default 90). Backfill and reconciliation cannot go further back than that.

**D3 detail:** GoModel stores `user_path = "/"` when none is set. That counts as "no user_path".

**R3: Lago accepts events for unknown subscriptions and never bills them.**
In Lago v1.53.0, ingestion doesn't check the subscription exists (`Events::CreateService`).
The task doc says such events are rejected; they are not. **Mitigation:** the sender
checks each subscription is active (`GET /api/v1/subscriptions/<id>`, cached 5 min)
before sending; if not, the events are rejected on our side and go to the dead-letter table.

**R4: Lago ignores usage from before a subscription started.**
Lago only attaches an event to a subscription whose `started_at` is at or before the
event's timestamp (`Events::PostProcessService`). Earlier events are accepted with no
error but never billed (found on 5 Oct: usage at 07:44 UTC, subscription started 08:09).
**Mitigation:** the sender compares each event's timestamp with the subscription's
`started_at`, and rejects earlier ones to the dead-letter table with the reason.
Test subscriptions are back-dated to the 1st of the month by `scripts/lago_setup.py`.

**R5: Lago deduplicates per subscription, not globally.**
The unique key is (organization, external_subscription_id, transaction_id). The same
usage row sent to a *different* subscription would be billed again. **Rule:** once a row
is sent, it must always go to the same subscription. Backfill (Step 9) must reuse
`usage_rows.external_subscription_id` for rows already sent, even if the key's label changed.

**Pricing note: package charges round up.** With the package model (size 1,000,000),
Lago bills `ceil(units / 1,000,000)` packages on each charge's **monthly total**
(`ChargeModels::PackageService`). 35 input + 3 output tokens cost $0.10 + $0.40 = $0.50.
With real traffic the overcharge is at most one package per metric per model per month.

## Verified behaviour

**Cached-token split matches GoModel (Step 6).** On real Ollama traffic (the same
prompt repeated, so Ollama reused 34 of 35 prompt tokens), `exporter dry-run`
produced 1 uncached + 34 cached input tokens per row, identical to GoModel's
own split in `GET /admin/usage/log` (`uncached_input_tokens` / `cached_input_tokens`).
Uncached + cached always equals `input_tokens`, so no token is billed twice.
## Takeover Step 1

**Durable intent:** freeze subscription, event codes, timestamps, token quantities and
properties in `deliveries` before HTTP. Retries and replay use that snapshot. Retain
acknowledgements across partial failures; drain pending records independently of the
source overlap. The `usage_rows` table remains the terminal summary, not the retry queue.

**Replay range:** `[start, end)`; dates are midnight UTC and timestamps require an
explicit timezone. Replay does not move the polling cursor. Legacy rows preserve
recorded destinations/accepted counts; original metric configuration is required
because old state did not save full payloads. See the review for upgrade limitations.

**Single writer:** polling, backfill and send-row acquire one PostgreSQL session advisory
lock per state database. This prevents competing configuration snapshots. Stop old
binaries before upgrade because they do not acquire this lock.

**Cache edge values:** negative/nonfinite raw cache counters become zero. GoModel's
maximum starts at zero; the original Python maximum could be negative when every
candidate was negative and incorrectly inflate uncached input.
