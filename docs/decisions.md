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