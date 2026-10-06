# Step 2 design and validation

Implementation is on `codex/reconciliation-status`, based on the completed Step 1
branch. It adds the following modules and state:

| Component | Responsibility |
| --- | --- |
| `exporter/lago_read.py` | Read-only, bounded Lago pagination; subscription/customer identity; current and past usage; strict numeric/time parsing. |
| `exporter/reconcile.py` | Exact-range event and payload audit, independent whole-billing-period usage checks, persisted evidence and explicit incomplete results. |
| `exporter/scheduler.py` | UTC grace, rolling lookback, restart-safe scheduling and retries. |
| `exporter/monitoring.py` | Read-only status UI/JSON, readiness and Prometheus metrics. |
| `exporter/state/store.py` and schema | Unique acknowledgement observations, export/reconciliation run status and retained reconciliation reports. |
| `exporter/cli.py` | `reconcile` and combined `serve` commands. |

The service does not write to GoModel or mutate Lago during reconciliation. It acquires
the same local writer lock as delivery so the exporter ledger stays stable. Source/API
unavailability produces an incomplete persisted result. The newest reporting period
is displayed even when an older catch-up finishes later; unresolved older periods
remain separately counted alerts.

## Upstream contracts checked

These are pinned Lago v1.53.0 source references, not assumptions taken from a mock:

- [Event controller](https://github.com/getlago/lago-api/blob/v1.53.0/app/controllers/api/v1/events_controller.rb) and [event query](https://github.com/getlago/lago-api/blob/v1.53.0/app/queries/events_query.rb): paginated range listing and inclusive timestamp bounds. Local audit normalizes to exclusive end.
- [Event serializer](https://github.com/getlago/lago-api/blob/v1.53.0/app/serializers/v1/event_serializer.rb): three-decimal timestamp serialization and original event properties. Millisecond-aligned audit bounds are required.
- [Usage controller](https://github.com/getlago/lago-api/blob/v1.53.0/app/controllers/api/v1/customers/usage_controller.rb) and [usage service](https://github.com/getlago/lago-api/blob/v1.53.0/app/services/invoices/customer_usage_service.rb): current billing periods, active subscriptions, and disabled charge caching for nonempty group filters. There is no public arbitrary-day/force-refresh parameter.
- [Charge calculation](https://github.com/getlago/lago-api/blob/v1.53.0/app/services/fees/charge_service.rb): group filters merge into each charge-filter aggregation. The parser selects the appropriate model bucket rather than summing other price buckets.
- [Usage serialization](https://github.com/getlago/lago-api/blob/v1.53.0/app/serializers/v1/customers/charge_usage_serializer.rb): decimal unit strings, metric metadata, filter values and filter-level units.
- [Historical usage query](https://github.com/getlago/lago-api/blob/v1.53.0/app/queries/past_usage_query.rb): paginated invoiced periods; unsuitable for pretending a finalized invoice is freshly recomputed daily usage.
- [Subscription serializer](https://github.com/getlago/lago-api/blob/v1.53.0/app/serializers/v1/subscription_serializer.rb): external customer ID and lifecycle information.

## Test gate

All Step 1 regressions remain in the gate. New tests cover:

- Three-way tokens across customers, models and cache types, including partial failures.
- Deleted, unexpected and individually changed events; row-level identity even when
  total token counts balance; frozen-source changes and missing/legacy evidence.
- Exact day boundaries, millisecond serialization, whole-month comparisons, days
  crossing billing periods, terminated subscriptions, missing/overlapping history.
- Cache bypass, unrelated price buckets, missing metrics, unavailable Lago,
  malformed numeric values, pagination and page caps, processing grace and recovery.
- Persisted scheduling across restarts, due checks under lock, rolling catch-up and retry.
- Idle versus outstanding-usage lag, capped scans, unique acknowledgement counts,
  dead-letter display, escaping, readiness, parseable Prometheus metrics and safe errors.
- CLI input/exit behavior and combined service worker startup/shutdown.

The HTML was visually inspected using a local fixture at a narrow viewport. Endpoint
and service tests exercise the real application with PostgreSQL in CI. Lago responses
are simulated using response shapes checked against the pinned source. Actual Lago
billing integration, the real outage demonstration and clean Compose startup remain
Step 3 gates; this step does not claim those have run.

Final CI results are recorded below after the gate completes.
