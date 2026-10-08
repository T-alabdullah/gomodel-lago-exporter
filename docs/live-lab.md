# Local Llama Usage Lab

**Windows:** use the [copy-paste Windows / WSL 2 instructions](live-lab-windows.md).

Start from the repository root with Docker Desktop running:

```sh
python3 scripts/run_live.py
```

Open http://localhost:8090. First startup downloads Ollama and the approximately
1.3 GB `llama3.2:1b` model. Inference is local CPU inference in Docker. Token counts
are measured by the model; cached tokens may be zero. No paid model API is used.

The optional Compose overlay changes the two GoModel providers to the real Ollama
server and starts a dedicated lab server. The normal exporter is stopped; the lab
owns a real `Runner` and `DailyScheduler` with the existing advisory lock. Existing
GoModel, exporter and Lago volumes are retained. Demo pricing adds Llama alongside
the two deterministic model filters; rates are placeholders with package rounding.

## Guided experiment

1. Leave automatic delivery paused and send a prompt as Acme.
2. Watch streamed model text and the observed pipeline stages.
3. Open Request Trace. Inspect the HTTP body, measured usage and exact source row.
4. Click **Prepare only**. Inspect the committed `deliveries.payload` in Databases.
5. Click **Deliver / replay**. Watch acknowledgements and the actual Lago events.
6. Click it again. The IDs and quantities stay the same; Lago deduplicates retries.
7. Enable **Delivery network fault**, send a new request, and deliver it. The
   sender uses an unreachable loopback endpoint; the frozen delivery stays pending.
   Restore delivery and replay. This does not stop or modify the Lago server.
8. Try Beta (user-path mapping), Ghost (unmapped/dead letter), or `ollama-ext`
   (excluded provider). These generate real rows and genuine billing decisions.
9. Wait at least two seconds after delivery, then reconcile the selected interval.
   The full billing-period check can surface issues in other existing records.
10. Resume automatic delivery. Observe the real cursor and worker status advance.

## Views and data

- **Chat:** each send is independent, with adjustable temperature and output limit.
  The backend consumes the real GoModel SSE stream; the browser polls the persisted
  partial answer and observed evidence approximately every 1.8 seconds.
- **Request Trace:** dedicated per-request GoModel API-key label `trace:<UUID>`
  establishes exact correlation, including when a request creates multiple rows.
  Each key remains in GoModel for this local lab. Its secret is used only in memory
  and is never returned to the browser or journaled. Key cleanup is manual.
- **Databases:** bounded, allowlisted, read-only PostgreSQL queries; the latest 100
  rows, real column types, SQL shown, full JSON on row selection. No arbitrary SQL.
- **Lago Billing:** live public API responses for selected events, metrics,
  subscriptions, current Llama usage and the package plan. No direct Lago DB writes.
- **Reconciliation:** calls the production reconciler and saves real reports.
  Unavailable evidence stays unavailable; unmapped rows remain visible as issues.
- **Settings:** sanitized runtime configuration, persisted pause/fault controls,
  generation settings, operational status and the latest cycle report.

The SQLite journal in `live-history` stores request/response bodies and control
settings. PostgreSQL remains authoritative for source and exporter delivery state.
The UI labels observations, not fabricated stage timings. Old requests remain
selectable after restart (latest 100 shown). Interrupted inference is marked as
interrupted; it is not automatically resubmitted and potentially billed twice.

## Local trust boundary

The server binds only `127.0.0.1:8090` on the host. It holds GoModel admin credentials
solely to create correlated API keys, plus exporter state and Lago API access.
API secrets and passwords are omitted from browser responses. Host checking,
same-origin mutation checks, a required action header, and a restrictive CSP protect
local actions. This is a single-user developer lab; do not expose it publicly.

## Stop / return to deterministic demo

**Fresh start:** Settings includes **Fresh start · clear all demo data**. Confirming
it clears every source usage row, exporter delivery/acknowledgement/dead-letter
record, cursor, audit report, chat history, and the local Lago billing database
and queued jobs. It restarts services and reprovisions the configured demo pricing,
Acme/Beta customers and subscriptions. Delivery starts paused with fault injection
off. Credentials and downloaded Llama weights are retained. Wait for active
inference to finish before resetting; the UI reconnects when the reset completes.

The optional `live-reset` helper has no published port and is the only lab service
with a Docker socket mount (privileged access to the local Docker engine). It runs
fixed reset operations against containers in this Compose project. The web app
queues requests through the shared history volume and retains its read-only source
database access. Do not use this reset helper with production data. If a reset
fails, rerun `python3 scripts/run_live.py` and retry the button; progress is saved
in the history volume. Merely starting the demo never clears data.

```sh
docker compose --env-file .env.demo -f docker-compose.yml -f docker-compose.live.yml stop live-lab live-reset ollama
docker compose --env-file .env.demo up -d --no-deps gomodel
```

The base Compose file routes GoModel back to `mock-provider`. Start the standard
exporter using the README workflow when desired. Do not run two independent
exporter services against the same state DB; the advisory lock will reject overlap.
No cleanup command deletes database volumes or the Llama weights.

## Implementation

`demo/live_app.py` supplies the journal, streaming inference proxy, safe inspectors,
and calls into production exporter modules. `demo/live_static/` contains plain
HTML/CSS/JavaScript with no CDN or frontend build dependency. `demo/live_setup.py`
provisions source read access and Llama pricing. `docker-compose.live.yml` and
`scripts/run_live.py` provide reproducible startup. The baseline deterministic
acceptance environment is still in `docker-compose.yml`.
