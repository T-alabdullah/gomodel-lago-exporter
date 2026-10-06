"""Command line entry point: `exporter <command>`."""

import json

import typer

from exporter.config import get_settings

app = typer.Typer(help="GoModel -> Lago usage exporter", no_args_is_help=True)


@app.command()
def config() -> None:
    """Print the effective configuration (secrets masked)."""
    settings = get_settings()
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = settings.model_dump(mode="json")
    for name in ("gomodel_db_url", "state_db_url"):
        parts = conninfo_to_dict(values[name])
        if "password" in parts:
            parts["password"] = "**********"
        values[name] = make_conninfo(**parts)
    print(json.dumps(values, indent=2))


@app.command("init-db")
def init_db() -> None:
    """Create the exporter's tables (safe to run again)."""
    from exporter.state.store import StateStore

    with StateStore(get_settings().state_db_url) as store:
        store.init_schema()
        counts = store.status_counts()
    print("State database ready.", counts)


@app.command()
def peek(limit: int = 20) -> None:
    """Print the oldest LIMIT usage rows the reader can see in GoModel."""
    from exporter.reader import UsageReader

    rows = UsageReader(get_settings().gomodel_db_url).read_after(None, limit)
    print(f"{'timestamp (UTC)':23} {'id':8} {'provider':11} {'user_path':20} "
          f"{'labels':18} {'in':>5} {'out':>5} cache")
    for r in rows:
        print(f"{r.timestamp.strftime('%Y-%m-%d %H:%M:%S.%f')[:23]:23} {r.id[:8]:8} "
              f"{r.provider_name or '-':11} {r.user_path or '-':20} "
              f"{','.join(r.labels) or '-':18} {r.input_tokens:>5} {r.output_tokens:>5} "
              f"{r.cache_type or '-'}")
    print(f"({len(rows)} rows)")


@app.command("dry-run")
def dry_run(limit: int = 50) -> None:
    """Show what the oldest LIMIT rows WOULD send to Lago. Sends nothing."""
    from exporter.contracts import MappingOutcome
    from exporter.events import build_events
    from exporter.mapper import map_row
    from exporter.reader import UsageReader

    settings = get_settings()
    rows = UsageReader(settings.gomodel_db_url).read_after(None, limit)
    totals = {outcome: 0 for outcome in MappingOutcome}
    event_count = 0
    for row in rows:
        result = map_row(row, settings)
        totals[result.outcome] += 1
        who = result.external_subscription_id or result.outcome.value.upper()
        print(f"{row.id[:8]}  {row.provider_name or '-':11} -> {who}  ({result.reason})")
        if result.outcome is MappingOutcome.MAPPED:
            for event in build_events(result, settings):
                event_count += 1
                print(f"            {event.code:24} {event.tokens:>6}  {event.transaction_id}")
    print(f"\n{len(rows)} rows: {totals[MappingOutcome.MAPPED]} mapped ({event_count} events), "
          f"{totals[MappingOutcome.NOT_BILLABLE]} not billable, "
          f"{totals[MappingOutcome.UNMAPPED]} unmapped. Nothing was sent.")


@app.command("send-row")
def send_row(row_id_prefix: str) -> None:
    """Replay one unambiguous usage UUID prefix through durable state."""
    import re
    from exporter.reader import UsageReader

    if not re.fullmatch(r"[0-9a-fA-F-]{1,36}", row_id_prefix):
        raise typer.BadParameter("Use a UUID or hexadecimal UUID prefix.")
    settings = get_settings()
    rows = UsageReader(settings.gomodel_db_url).find_by_id_prefix(row_id_prefix.lower())
    if len(rows) != 1:
        raise typer.Exit(_fail(f"{len(rows)} rows match; use a longer or exact UUID."))
    _replay(lambda runner: runner.send_row(rows[0]))


def _fail(message: str) -> int:
    typer.echo(message, err=True)
    return 1


@app.command()
def run(once: bool = typer.Option(False, "--once", help="Run one cycle and exit.")) -> None:
    """Run the export loop (Ctrl+C to stop)."""
    import logging

    from exporter.lago_client import LagoAuthError, LagoClient
    from exporter.reader import UsageReader
    from exporter.runner import Runner
    from exporter.sender import Sender
    from exporter.state.store import ExporterBusyError, StateStore

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # don't log every HTTP request
    settings = get_settings()
    with StateStore(settings.state_db_url) as store:
        store.init_schema()

    client = LagoClient(settings.lago_api_url, settings.lago_api_key.get_secret_value(),
                        settings.http_timeout_seconds)
    runner = Runner(
        settings,
        open_store=lambda: StateStore(settings.state_db_url),
        reader=UsageReader(settings.gomodel_db_url),
        sender=Sender(client, settings),
    )
    try:
        if once:
            report = runner.run_cycle()
            print(report.summary())
            if report.waiting_for_retry or report.failed or report.unmapped:
                raise typer.Exit(1)
        else:
            logging.info("exporter running; one cycle every %ss (Ctrl+C to stop)",
                         settings.poll_interval_seconds)
            runner.run_forever()
    except (LagoAuthError, ExporterBusyError) as err:
        raise typer.Exit(_fail(str(err)))
    except KeyboardInterrupt:
        logging.info("stopped")
    finally:
        client.close()


def _parse_boundary(value: str):
    from datetime import datetime, timezone

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as err:
        raise typer.BadParameter("Use YYYY-MM-DD or an ISO timestamp with timezone.") from err
    if parsed.tzinfo is None:
        if len(value) != 10:
            raise typer.BadParameter("Timestamps must include a timezone; dates mean midnight UTC.")
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _replay(action) -> None:
    from exporter.lago_client import LagoAuthError, LagoClient
    from exporter.reader import UsageReader
    from exporter.runner import Runner
    from exporter.sender import Sender
    from exporter.state.store import ExporterBusyError, StateStore

    settings = get_settings()
    with StateStore(settings.state_db_url) as store:
        store.init_schema()
    client = LagoClient(settings.lago_api_url, settings.lago_api_key.get_secret_value(),
                        settings.http_timeout_seconds)
    runner = Runner(settings, lambda: StateStore(settings.state_db_url),
                    UsageReader(settings.gomodel_db_url), Sender(client, settings))
    try:
        report = action(runner)
        print(report.summary())
        if report.waiting_for_retry or report.failed or report.unmapped:
            raise typer.Exit(1)
    except (LagoAuthError, ExporterBusyError) as err:
        raise typer.Exit(_fail(str(err)))
    finally:
        client.close()


@app.command()
def backfill(start: str, end: str) -> None:
    """Replay [START, END); dates mean midnight UTC, END is excluded.

    Does not move the polling cursor. Repeat the same command after an outage.
    Previously prepared rows retain their original subscription and payload.
    """
    start_time, end_time = _parse_boundary(start), _parse_boundary(end)
    if start_time >= end_time:
        raise typer.BadParameter("START must be before END.")
    _replay(lambda runner: runner.backfill(start_time, end_time))


@app.command()
def reconcile() -> None:
    """Compare GoModel, exporter and Lago totals. (Step 10)"""
    raise typer.Exit(_not_yet("reconcile", 10))


def _not_yet(name: str, step: int) -> int:
    typer.echo(f"'{name}' is not implemented yet (Step {step}).", err=True)
    return 1


if __name__ == "__main__":
    app()