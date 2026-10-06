# Operating delivery and reconciliation

Delivery, operational visibility and independent reconciliation run together through
`exporter serve`. See the README for the complete Compose stack and
[deployment runbook](deployment.md) for startup, persistence and real-stack acceptance.

## Start and inspect

Stop older exporter processes before upgrading, preserve the same state database,
then run:

```bash
exporter init-db
exporter serve
```

`serve` runs the exporter loop, daily reconciliation scheduler and HTTP server in
one process. It binds to `127.0.0.1:8000` by default. `EXPORTER_STATUS_HOST` and
`EXPORTER_STATUS_PORT` control the bind address; use an authenticated reverse proxy
before exposing customer usage outside a trusted network. The interface is read-only.
`exporter run` still runs delivery alone and does not start a scheduler or web server.
Do not run it alongside `serve` unless you intentionally want competing workers.

| Endpoint | Purpose |
| --- | --- |
| `/` | Status page: cursor lag, last completed run, unique event acknowledgements today, failures, pending deliveries, open dead letters and latest reconciliation. Refreshes every 30 seconds. |
| `/status` | JSON representation of status and the latest report. Errors do not expose connection strings. |
| `/health` | Readiness, not merely process liveness. 200 means dependencies and operational checks are healthy; 503 includes reasons such as stale worker, billing lag, open dead letters or incomplete/mismatched reconciliation. |
| `/metrics` | Prometheus exposition with low-cardinality operational metrics and `exporter_alert{reason="..."}` for active alerts. |

Examples of alert expressions: `exporter_alert{reason="billing_lag"} == 1`,
`exporter_dead_letters > 0`, `exporter_unresolved_reconciliation_periods > 0`.
Also alert on a missing/down scrape target, because a stopped process cannot emit
its own alert. Hooking these expressions to an external notification destination
belongs to deployment; the service itself emits logs, readiness reasons and metrics.

Cursor lag is `now - cursor.timestamp`, as requested. It increases when there is
no traffic. The billing-lag alert instead uses the oldest outstanding source/pending
row and `EXPORTER_LAG_ALERT_SECONDS` (900). The source scan covers the normal overlap
and forward backlog; old late rows beyond that window are found by reconciliation.
It stops at `EXPORTER_MONITOR_MAX_ROWS` (10,000) and raises an explicit incomplete-scan
alert instead of claiming the backlog is empty.

Events today means distinct event identities first observed as acknowledged during
the current UTC day, including recovery of a remotely accepted duplicate. It is not
HTTP attempts, token count, invoice count, or a claim about remote ingestion time.
Replaying an already observed identity does not increment it. Pre-upgrade exact
acknowledgement times are unavailable and cannot be reconstructed retrospectively.

## Reconcile on demand

```bash
exporter reconcile
exporter reconcile --start 2026-10-05 --end 2026-10-06
```

The default is yesterday UTC. Start is inclusive; end is exclusive. Explicit
timestamps require a timezone. Lago serializes event timestamps to milliseconds,
so finer-than-millisecond audit bounds are reported incomplete. Backfill and
reconciliation are different commands: reconciliation never sends or deletes events.

Exit 0 means `matched`. Exit 1 means `mismatch`, `incomplete`, or a busy writer.
The report is persisted in `reconciliation_runs` and includes its ID, period,
source/acknowledged/Lago token counts by subscription, customer, model and token kind,
row IDs, exclusions, issues and separately scoped billing-period checks.

- **Matched:** the available event evidence and supported billing-period evidence agree.
- **Mismatch:** evidence was retrieved, but rows, payloads or counts differ, or usage
  remains unmapped, rejected or pending.
- **Incomplete:** an API/database response is unavailable or malformed, pagination is
  unstable/capped, the period has not settled, recent deliveries may still be processing,
  source retention is exceeded, historical breakdowns are ambiguous, or legacy state
  cannot supply its original payload. Unknown Lago totals remain `null`, not zero.

A report can be incomplete and still contain concrete mismatches. Inspect its issues;
fixing availability does not automatically resolve those mismatches.

## What is actually compared

The exact requested range is read from GoModel with the normal reader/mapper/cache
split. Exporter quantities include all acknowledged events, including partially
failed deliveries, rather than only rows marked `sent`. Lago's paginated `/events`
API supplies recorded event quantities for the same range. The audit also compares
transaction identity, timestamp (at Lago's millisecond precision), metric code and
properties. This catches swapped/altered events even when totals happen to balance.
Unexpected token events are included in the differences.

Recorded Lago events alone do not prove billable usage. For every subscription
involved, reconciliation resolves its external customer ID and obtains the applicable
Lago billing periods. It reads source and exporter quantities for each **whole returned
period** and compares them with Lago's usage units. A daily total is never compared
to a monthly total. A day crossing periods results in separate checks.

For current usage, a nonempty `filter_by_group={"model":["..."]}` disables Lago's
charge cache in the pinned version. The parser selects the matching model price
bucket (or unambiguous default), rather than summing unrelated buckets in the
response. Historical usage is taken from paginated invoice usage periods and must
have a usable per-model breakdown. Duplicate/overlapping periods are incomplete.
Current snapshots and historical invoiced usage are labeled separately in the report.
Token sums are checked; package rounding and currency amounts are not reconciled.

The supported setup is the task's nonrecurring `sum_agg` token metrics and model price
filters. Unsupported/ambiguous response layouts cannot produce a successful proof.
Metric codes must remain consistent with existing billing configuration and legacy
state. Changing a frozen payload requires a billing correction, not ordinary replay.

An active subscription is required by the existing sender. Reconciliation can inspect
terminated subscriptions through historical invoice evidence, but this does not make
backfill into terminated subscriptions automatic or reopen finalized invoices.

## Daily scheduling and recovery

`serve` schedules completed UTC days after a default 15-minute grace period. It catches
up one period per tick, newest first, for a configurable seven-day lookback. Completed
matches are not duplicated on same-day restarts; the lookback is rechecked on later
days to catch late rows and later data loss. Mismatched/incomplete reports retry after
five minutes. A durable under-lock due check prevents duplicate competing schedulers.
Older unresolved periods remain alerts even if the newest period matched.

Configuration:

| Setting | Default | Meaning |
| --- | --- | --- |
| `EXPORTER_RECONCILIATION_DELAY_SECONDS` | 900 | Grace after day end and after observed delivery acknowledgement. |
| `EXPORTER_RECONCILIATION_RETRY_SECONDS` | 300 | Retry delay for mismatched/incomplete scheduled periods. |
| `EXPORTER_RECONCILIATION_LOOKBACK_DAYS` | 7 | Daily rolling catch-up/recheck range. Older dates need explicit reconciliation. |
| `EXPORTER_RECONCILIATION_PAGE_SIZE` | 100 | Lago pagination size, maximum 100. |
| `EXPORTER_RECONCILIATION_MAX_PAGES` | 10000 | Safety cap; reaching it makes evidence incomplete. |
| `EXPORTER_SOURCE_RETENTION_DAYS` | 90 | Must match GoModel retention; older scopes cannot be proved complete. |
| `EXPORTER_HEARTBEAT_STALE_SECONDS` | 300 | Export cycle inactivity threshold. |
| `EXPORTER_MONITOR_MAX_ROWS` | 10000 | Source backlog inspection cap per status request. |

Delivery and reconciliation share the PostgreSQL writer lock to prevent the local
ledger changing during an audit. A long audit can therefore delay exports. This is
a conservative, single-writer implementation for the task's environment; high-volume
production use should move to a versioned snapshot/lease design and tune the bounded
read limits. Source and Lago remain separate systems, so concurrent upstream changes
can yield transient mismatches; rerun after the grace period. An OS-killed audit leaves
no completed report and is picked up on restart.

## Repair runbook

1. Inspect `/status` or the persisted report and dead-letter row IDs. Keep the state
   database, original IDs and payloads intact. Do not delete `usage_rows` or `deliveries`
   to force a resend: that removes billing identity protections.
2. For **unmapped** usage without a frozen intent, correct the trusted key/source
   mapping, then backfill the affected range. Conflicting labels require an explicit
   mapping decision. Reconciliation identifies source-versus-frozen mapping changes.
3. For a **missing subscription**, provision the original intended subscription with
   the correct lifecycle dates, then replay. A wrong frozen destination must be reviewed
   as a billing correction; merely changing a label will not redirect its replay.
4. For **temporary API failure**, restore Lago/configuration and let pending deliveries
   retry. The scheduler rechecks incomplete/mismatched periods. Recent acknowledgements
   remain incomplete until the grace period ends.
5. For a **missing Lago event**, confirm it is missing rather than unprocessed or outside
   the billing period. For an active subscription and open billing period, replay the
   original range, preserving identity, then rerun reconciliation. Review finalized
   invoice effects before replay into closed periods.
6. For a **wrong token payload, wrong customer, wrong metric, or finalized invoice**,
   retain the mismatch and audit evidence and review the intended correction in Lago.
   Ordinary replay deliberately cannot rewrite already billed facts. This service
   does not issue credit notes, delete events or alter invoices automatically.
7. For **missing source/legacy payload/retention**, recover the relevant source or
   backup if available. Otherwise leave the result incomplete. The exporter cannot
   reconstruct usage dropped before GoModel recorded it.
8. Reconcile again. A newer matched report resolves that period's alert; historical
   reports remain available in the database for audit.
