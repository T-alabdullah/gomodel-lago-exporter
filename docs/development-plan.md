# Development plan

This plan supersedes the fourteen implementation slices in the original handover.
Each numbered item below is one major step and one future prompt. Minor items are
implementation checkpoints, not separate prompts. Finish the tests for a step
before starting the next. A skipped database test is not a passing gate.

## 1. Durable delivery and safe backfill

**Complete:** 116 tests passed with no skips; see [validation](step-1-validation.md).

- Review all original files, eight commits, requirements and pinned upstream contracts.
- Save immutable subscription and complete event payloads before HTTP; retain pending
  work outside the overlap window and after source deletion.
- Share one durable delivery path across polling, date-range backfill and send-row.
- Keep backfill independent of the polling cursor; preserve legacy recorded destinations
  and accepted quantities; resolve repaired dead letters.
- Serialize writers, correct cache-token edge cases, tighten duplicate classification,
  handle malformed subscription responses conservatively and mask DSN passwords.
- Establish CI with PostgreSQL and a no-skips gate.

**Gate:** original regression suite; repeated range replay; exact time boundaries;
partial delivery; outage/recovery; crash between acceptance and commit; actual SIGKILL
and restart; changed mapping/configuration; deleted source; dead-letter recovery;
writer exclusion; actual read-only database login; CLI validation; package build.
Lago HTTP semantics are simulated in this step and checked against pinned source.
Full real-Lago acceptance remains the final step's gate.

## 2. Reconciliation and operational visibility

**Complete:** 182 tests passed with no skips; see [design and validation](step-2-design.md).

- Implement a persisted reconciliation report and daily scheduler, with an explicit UTC
  period and consistent subscription/customer, model and token-kind dimensions.
- Compare GoModel billable expectations, exporter acknowledgements (including accepted
  events in partial/failed deliveries), and Lago-reported usage. Never sum only
  `status = 'sent'`; partial deliveries contain real accepted usage too.
- Verify Lago v1.53.0 API period limits, pagination, filtered model breakdowns, processing
  delay and cache refresh. A current-month aggregate is not proof of a previous day's
  total. Use supported event inspection/usage endpoints to make periods comparable;
  report unavailable evidence as incomplete, not a zero difference.
- Persist mismatch row IDs and distinguish unmapped, excluded, pending, rejected,
  missing-Lago and mismatched-token cases. Include legacy migration limitations.
- Add status UI, health, Prometheus metrics, persisted last-run/heartbeat and daily
  counts; expose pending deliveries and latest reconciliation. Preserve the requested
  now-minus-cursor metric while preventing no-traffic false alerts using backlog data.
- Provide a documented repair workflow for permanent failures. Review destination or
  payload corrections explicitly; ordinary replay must not rewrite billed identities.
- Validate historical/terminated subscription behavior before promising automatic
  backfill into closed periods.

**Gate:** deliberately missing Lago event is detected with relevant rows; correct
three-way totals for cache splits, multiple customers/models and partial delivery;
billing-period boundary cases; pagination; unavailable Lago; refresh/processing delay;
scheduler restart; health/metrics/UI; visible unmapped rows and lag alert behavior.
Step 1 regressions must stay green.

## 3. Complete environment and acceptance demonstration

**Complete:** 201 regression tests with zero skips and all 11 real-stack acceptance
scenarios passed. See [Step 3 validation](step-3-validation.md).

- Add exporter container, pinned Lago API/worker/Postgres/Redis components, unattended
  organization/bootstrap and read-only-role provisioning after GoModel migration.
- Replace model downloads with a deterministic mock provider supporting streaming
  and ordinary responses. Add sustained mixed traffic across acme, beta, ghost and
  the non-billable provider; preserve a realistic optional Ollama path if useful.
- Verify setup-script idempotence, model/price configuration, secret handling, startup
  ordering, state persistence, dependency reproducibility and upstream schema checks.
- Run a fresh-clone Compose acceptance suite with real GoModel and Lago: outage under
  load, catch-up, mid-batch process death, repeated backfill, late insertion, unmapped
  usage, deliberately removed Lago event and zero-difference reconciliation after recovery.
- Finish operational README, failure-mode and recovery runbook, architecture explanation
  and a timed 15–20 minute presentation/demo outline.

**Gate:** clean `docker compose up`; automated setup rerun; all previous tests; real
end-to-end acceptance scenarios; operational instructions reproduced from scratch.
Do not declare project complete until this gate passes. No test suite proves that
software has no possible bugs; report precisely what was exercised and what remains.
