# Step 3 implementation and acceptance gate

Step 3 completes the environment and delivery artifacts. Its gate consists of the
PostgreSQL regression workflow plus a separate fresh-stack acceptance workflow.
Final run links and results are recorded after the full acceptance gate passes.

## Environment

- Stock GoModel 0.1.99 with its real PostgreSQL usage migrations.
- Real Lago v1.53.0 API, Sidekiq worker, clock, PostgreSQL event store and Redis.
- A nonroot exporter image built with fixed Python dependencies; a separate state DB.
- A deterministic model provider supporting discovery, plain completions and SSE
  streaming with prompt-cache usage. Two models have different token quantities/prices.
- Automatic organization, token metrics, package plan, acme/beta subscriptions and
  acme/beta/ghost GoModel keys. Private generated secrets persist across setup reruns.
- A source SELECT-only role provisioned after upstream migration. Startup validates
  actual column types and confirms that the role cannot DELETE even after explicitly
  disabling PostgreSQL's default read-only transaction setting.

## Automated acceptance scenarios

The harness starts with empty source/Lago data and runs against actual service APIs.
Only model inference is mocked. Source-fixture mutations and Lago event removal are
isolated test operations; the exporter still has no source-write credentials.

1. Re-run bootstrap successfully and validate source schema/read-only permissions.
2. Generate mixed traffic: acme by label, beta by user_path, ghost unmapped; two models,
   streaming/plain responses and both billable/excluded provider names. Check every
   successful request produces a source row and cached tokens normalize correctly.
3. Verify unmapped rows in persisted dead letters, status JSON and HTML. Repair the
   demo fixtures, backfill and require zero differences in events and billed units.
4. Replay the same range twice; compare Lago identities, acknowledgement counts and
   cursor before/after, not just HTTP exit codes.
5. Insert a real-schema row behind the cursor inside the overlap, then an older row
   outside it; require polling and explicit backfill respectively to deliver them.
6. Stop Lago API for at least ten seconds while sending mapped traffic; require durable
   pending records and complete catch-up on recovery.
7. Kill a standalone exporter with SIGKILL immediately after a real successful Lago
   batch response and before local acknowledgement; require pending intents, unchanged
   identities on retry and zero-difference reconciliation after restart.
8. Delete one event in the isolated Lago database; require its source UUID in a
   mismatch report. Restore through normal frozen-payload backfill and audit again.
9. Recreate services with volumes retained; require stable Lago identities and local
   acknowledgement counts, accessible status/metrics/HTML, and recovered readiness.

`acceptance-results.json` preserves assertion-backed scenario results and reports.
CI also uploads container logs and resolved image identities, without the credentials
file. The harness refuses an already populated demo source or Lago event store.

## Integration findings

The real API exposed an empty-pagination convention absent from the original mocks:
Lago returns `current_page: 0` for an empty result. The read adapter now accepts that
only for a complete, empty first response, with regression tests rejecting inconsistent
zero-page metadata. Existing pagination caps, consistency checks and unavailable-evidence
behavior remain in force.

Processing grace was also exercised: auditing through the current instant correctly
returns incomplete. Acceptance selects a settled end boundary rather than disabling
the grace check. Test fixture repair uses unmapped row identities, independent of how
GoModel serializes empty labels.

## Scope and reproducibility

The local machine has no Docker engine; container tests run on GitHub-hosted Linux
amd64. This verifies real pinned GoModel/Lago services, not the Step 1/2 Lago HTTP mocks.
It is a bounded functional/load test, not a production capacity benchmark or a proof
that unrecorded GoModel requests can be reconstructed. Historical finalized invoice
corrections, taxes/currency correctness, external alert routing and other architectures
are outside this gate. Operations and presentation instructions are in the README,
`docs/deployment.md`, `docs/operations.md` and `docs/presentation.md`.
