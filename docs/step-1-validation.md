# Step 1 validation

The first full PostgreSQL run passed on GitHub Actions:
[run 37429145942](https://github.com/T-alabdullah/gomodel-lago-exporter/actions/runs/37429145942).
It independently ran the unmodified `3452566` baseline (**76 passed**) and the expanded
suite (**113 passed**), with **zero skipped tests**. The final revision adds stronger
restricted-login coverage, migration-acknowledgement regressions and CLI coverage;
its result will be recorded here after its gate completes.

The no-skips gate is `.github/workflows/tests.yml`: Ubuntu, Python 3.12 and PostgreSQL
16. Dependencies are installed into the runner, the original baseline is extracted
from Git, and the current suite runs with `--require-db`. `pip check`, source and
wheel builds and Python compilation also passed. JUnit results are retained as the
Actions `test-results` artifact.

Local unit testing is additional evidence only. The first unmodified local run was
49 passed / 27 skipped. Docker/Postgres were absent on this machine; an isolated
PostgreSQL build succeeded but macOS sandbox shared-memory restrictions prevented
startup. Therefore the database gate was run on GitHub, not claimed from skipped
local tests. The initial CI attempt failed at container health-check argument parsing;
that was corrected before the passing run.

## Behaviors covered

- All original reader, mapping, cache split, sender, state and runner regressions.
- Repeated backfill with identical Lago event contents, bounded pages, inclusive start,
  exclusive end, an all-zero UUID at the start, empty range and invalid/timezone-less input.
- Unchanged polling cursor during replay, including replay beyond the current cursor.
- Unmapped-row repair and dead-letter closure; missing subscription becoming available.
- Immutable destination, token counts and metric codes after crash/source/config changes.
- Partial batch acceptance followed by retry; accepted counts retained through failure.
- Pending delivery after source deletion and after overlap configuration changes.
- Outage then catch-up, with resumable interrupted backfill.
- Concurrent writer exclusion and lock release after failure.
- An actual child process killed with SIGKILL after the HTTP server accepted events,
  followed by restart against PostgreSQL; no extra events after mapping/token changes.
- Legacy subscription/quantity reuse, including a later rejected replay.
- Read-only source access, secret redaction, malformed subscription responses,
  strict duplicate classification, URL-encoded subscription IDs and cache edge values.

Lago is represented by a deterministic HTTP fake; the process-death test uses a real
local HTTP server around that fake and an actual exporter child process. PostgreSQL
is real. This proves local durability/replay against the stated HTTP contract; it does
not prove that real Lago processed events into billing usage. Live GoModel/Lago outage,
deleted-event reconciliation, UI/metrics and clean Compose acceptance remain the
explicit gates in Steps 2 and 3. No production billing events were sent.
