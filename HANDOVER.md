> **Takeover update (6 October 2026):** This is the historical Steps 1–8 handover.
> Follow [the three-step plan](docs/development-plan.md) and [repository review](docs/repository-review.md)
> for current behavior. Step 1 adds durable intent before HTTP and safe replay; the
> original retry and send-row descriptions below no longer describe the current code.

# Handover: Steps 1–8 done → Steps 9–14 are yours

The exporter works end to end. GoModel usage is read, mapped to a Lago
subscription, turned into events, sent to Lago, and recorded, every 10 seconds,
with no double billing and no lost usage. **76 automated tests** pass.

It has been run against real GoModel 0.1.99 and real Lago v1.53.0, including a
live Lago outage (stopped about 60s while sending traffic): the exporter retried,
caught up by itself, and nothing was lost or billed twice.

---

## 1. Run it from scratch

**You need:** Docker Desktop (Settings → Resources → Memory ≥ 8 GB) and Python 3.12
(we use `conda create -n gomodel-exporter python=3.12`).

```bash
# Python
conda activate gomodel-exporter
pip install -e ".[dev]"
cp .env.example .env

# GoModel + its Postgres + Ollama + the exporter's Postgres
docker compose up -d
exporter init-db
docker compose exec -T gomodel-db psql -U postgres -d gomodel < scripts/gomodel_readonly_role.sql
python scripts/gomodel_setup.py      # API keys acme / beta / ghost -> .gomodel-keys.json
```

**Lago** runs from its own folder next to this repo. See README → "Running the dev stack":
clone, `git checkout v1.53.0`, add the RSA key to its `.env`, `docker compose up -d`,
sign up at http://localhost, then copy **Developers → API keys** (not the Organization ID)
into `EXPORTER_LAGO_API_KEY` in this repo's `.env`, and run:

```bash
python scripts/lago_setup.py         # metrics, plan, customers acme + beta (safe to re-run)
```

Then start the exporter:

```bash
exporter run                         # Ctrl+C to stop. `exporter run --once` = one cycle.
```

## 2. Check it works (5 minutes)

1. `python -m pytest -q` → **76 passed**.
2. Terminal 1: `exporter run`. Terminal 2: `python scripts/smoke_traffic.py`.
   Within ~15 s terminal 1 prints:
   `... 4 sent (… events), 3 not billable, 2 unmapped, 0 failed, 0 waiting for retry`
   (2 acme + 2 beta sent; 3 `ollama-ext` rows not billable; 2 ghost rows unmapped).
3. Lago → Customers → Acme / Beta → subscription → Usage → **Refresh**: token counts go up.
4. Dead letters:
```bash
   docker compose exec exporter-db psql -U postgres -d exporter -c \
     "SELECT left(usage_row_id::text,8) AS row, reason, left(error,70) AS error FROM dead_letters WHERE resolved_at IS NULL;"
```
5. Outage drill: in the Lago folder `docker compose stop api`, send traffic, wait ~20 s,
   `docker compose start api`. You'll see retries, then `N waiting for retry`, then `N sent`.

Handy commands: `exporter peek` (rows the exporter can read), `exporter dry-run`
(what would be sent; sends nothing), `exporter send-row <id-prefix>` (send one row; testing only).

## 3. How it's built

```
GoModel usage table ──reader──▶ UsageRow ──mapper──▶ MappingResult ──events──▶ LagoEvent ──sender──▶ SendResult
                                                                                                 │
                         state DB (cursor, usage_rows, dead_letters) ◀──────── runner ──────────┘
```

| File | Job |
| --- | --- |
| `exporter/contracts.py` | The data passed between stages. **Start reading here.** |
| `exporter/config.py` | Every setting (`EXPORTER_*` env vars). Defaults match `docs/decisions.md`. |
| `exporter/reader.py` | Reads GoModel's `usage` table, oldest first, keyset paging, read-only login. `overlap_start()` = cursor − 10 min. |
| `exporter/mapper.py` | Who pays: billable provider? → `lago:` label → `user_path` last segment → else unmapped. |
| `exporter/events.py` | Up to 3 events per row (`:in`, `:cached`, `:out`); cached split copied from GoModel's `EntryInputSegments`. |
| `exporter/lago_client.py` | HTTP to Lago; classifies answers (accepted / duplicate / rejected). 401/403 stops everything. |
| `exporter/sender.py` | Subscription checks, batches of 100, one-by-one fallback, retries with backoff + jitter. |
| `exporter/runner.py` | The loop. One transaction per page: record outcomes + dead letters + move cursor. |
| `exporter/state/` | The exporter's own DB: `schema.sql` + `store.py`. |
| `scripts/` | Dev setup: GoModel keys, Lago config, read-only role, smoke traffic, pricing file. |

**The two rules that give the guarantees** (both in `runner.py`):
- A row whose events got `RETRY` is **not recorded**, and the cursor **never moves past it**.
- Recording + dead letters + cursor move happen in **one transaction**. A crash re-sends,
  and Lago answers "duplicate".

## 4. Thursday criteria → what proves them today

| Criterion | Automated test (`tests/test_runner.py` unless noted) | Shown live today |
| --- | --- | --- |
| No double billing | `test_running_again_sends_nothing_twice`, `test_crash_between_send_and_save_never_double_bills`, `test_sender.py::test_batch_with_some_duplicates_still_delivers_the_new_ones` | ✅ row sent twice, counted once in Lago |
| No lost usage | `test_lago_outage_loses_nothing` | ✅ Lago stopped ~60 s, exporter caught up |
| Late rows caught | `test_late_row_behind_cursor_is_still_sent`, `test_reader.py::test_late_row_behind_cursor_is_found_by_overlap_window` | — |
| Unmapped usage visible | `test_first_cycle_handles_every_kind_of_row` | ✅ ghost rows in `dead_letters` |
| Reconciliation | **Step 10** | — |
| `docker compose up` from clean clone | **Step 12** | — |
| README | **Step 14** | — |

## 5. Things we learned the hard way (details in `docs/decisions.md`)

- **R1** Keys without a bound user path accept a client-supplied `X-GoModel-User-Path`.
  Every billable key needs a `lago:` label or a bound `user_path`.
- **R3** Lago **accepts** events for unknown subscriptions and never bills them.
  The sender checks subscriptions first.
- **R4** Lago **ignores** usage from before a subscription's `started_at`.
  The sender checks this too, and dead-letters it with the reason. (The 4 `failed` rows
  from 07:44 on 5 Oct are this case. They are expected, and a good demo of "visible, not lost".)
- **R5** Lago dedups per **subscription**: a row re-sent to a *different* subscription is billed again.
- **Package pricing rounds up** to whole packages on each monthly total.

## 6. Where Steps 9–14 plug in

**Step 9: Backfill.** `UsageReader.read_after(Cursor(start, MIN_ID), limit, until=end)`
already reads a date range. The runner **skips rows already in `usage_rows`**, so backfill needs
a "force" path: re-send every mapped row in the range (duplicates are safe), but **reuse
`usage_rows.external_subscription_id` for rows already sent** (R5). Re-recording a row
updates it in place (`attempts + 1`), and a successful send closes its dead letter
(`resolve_dead_letter`). The test to write: run a range twice, Lago totals unchanged.

**Step 10: Reconciliation.**
- *Exporter side*: `SELECT external_subscription_id, model, sum(input_tokens_sent), sum(cached_input_tokens_sent), sum(output_tokens_sent) FROM usage_rows WHERE status = 'sent' GROUP BY 1, 2`.
- *GoModel side*: sum the `usage` table for the same period, mapped with `map_row()` +
  `input_segments()` so the numbers are comparable.
- *Lago side*: `GET /api/v1/customers/<external_customer_id>/current_usage?external_subscription_id=<id>`
  (current billing period; check the per-model filter breakdown in the response).
- For "deleted Lago event shows as a mismatch": Lago's API has no event delete in v1.53.0,
  so delete from Lago's Postgres (`events` table; db/user `lago`, password `changeme`),
  then click Refresh on the usage page (Lago caches usage).

**Step 11: Status page, metrics, health.**
`store.status_counts()`, `store.open_dead_letters()`, and `store.get_cursor()` are ready.
- *Lag*: `now − cursor.timestamp` grows when there's simply **no traffic**. Consider
  "newest GoModel row − cursor" instead, or only alert when unprocessed rows exist.
  The threshold is `lag_alert_seconds` (900).
- *Last run* isn't stored yet. `runner.run_forever()` is the place to record a heartbeat
  (the `CycleReport` has all the numbers).

**Step 12: Full `docker compose up`.**
- Exporter image: `pip install .`. `pyproject.toml` already ships `state/*.sql`.
- `gomodel_readonly_role.sql` must run **after** GoModel has created its `usage` table
  (a one-shot service that waits for it).
- Lago v1.53.0's compose supports `LAGO_CREATE_ORG=true` with `LAGO_ORG_USER_EMAIL`,
  `LAGO_ORG_USER_PASSWORD`, `LAGO_ORG_NAME`, and `LAGO_ORG_API_KEY`, so the org and API key
  can be created on startup with no manual sign-up (see lago-api `scripts/migrate.sh`).
- A mock model provider can replace Ollama for speed. It must return `usage` in both
  streaming and non-streaming responses.

**Step 13: Failure tests.** Most criteria are covered (section 4). What's left: reconciliation
catching a deleted event, backfill twice, and a live `kill -9` of `exporter run` mid-batch.

**Step 14: README + runbook.** `docs/decisions.md` already holds every decision and risk.

## 7. Walkthrough agenda (30 min)

1. **5 min:** this file, sections 1–2. Run the 5-minute check together.
2. **10 min:** follow one row through the code: `contracts.py` → `reader` → `mapper` → `events` → `sender` → `runner`.
3. **5 min:** the two guarantee rules in `runner.py`, and the crash test.
4. **5 min:** R3 / R4 / R5 and why the sender checks subscriptions.
5. **5 min:** section 6. Agree on how backfill forces a re-send.