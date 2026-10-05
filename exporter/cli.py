"""Command line entry point: `exporter <command>`."""

import json

import typer

from exporter.config import get_settings

app = typer.Typer(help="GoModel -> Lago usage exporter", no_args_is_help=True)


@app.command()
def config() -> None:
    """Print the effective configuration (secrets masked)."""
    settings = get_settings()
    print(json.dumps(settings.model_dump(mode="json"), indent=2))


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
    """Send ONE usage row's events to Lago (for testing; safe to repeat)."""
    from exporter.contracts import MappingOutcome
    from exporter.events import build_events
    from exporter.lago_client import LagoClient
    from exporter.mapper import map_row
    from exporter.reader import UsageReader
    from exporter.sender import Sender

    settings = get_settings()
    rows = UsageReader(settings.gomodel_db_url).read_after(None, 10_000)
    matches = [r for r in rows if r.id.startswith(row_id_prefix)]
    if len(matches) != 1:
        raise typer.Exit(_fail(f"{len(matches)} rows match {row_id_prefix!r}; give a longer prefix."))
    result = map_row(matches[0], settings)
    if result.outcome is not MappingOutcome.MAPPED:
        raise typer.Exit(_fail(f"Row is {result.outcome.value}: {result.reason}. Nothing to send."))

    client = LagoClient(settings.lago_api_url, settings.lago_api_key.get_secret_value(),
                        settings.http_timeout_seconds)
    try:
        for r in Sender(client, settings).send(build_events(result, settings)):
            print(f"{r.event.transaction_id}  {r.outcome.value.upper()}  {r.error or ''}")
    finally:
        client.close()


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
    from exporter.state.store import StateStore

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
            print(runner.run_cycle().summary())
        else:
            logging.info("exporter running; one cycle every %ss (Ctrl+C to stop)",
                         settings.poll_interval_seconds)
            runner.run_forever()
    except LagoAuthError as err:
        raise typer.Exit(_fail(str(err)))
    except KeyboardInterrupt:
        logging.info("stopped")
    finally:
        client.close()


@app.command()
def backfill(start: str, end: str) -> None:
    """Re-send usage between START and END (ISO dates). (Step 9)"""
    raise typer.Exit(_not_yet("backfill", 9))


@app.command()
def reconcile() -> None:
    """Compare GoModel, exporter and Lago totals. (Step 10)"""
    raise typer.Exit(_not_yet("reconcile", 10))


def _not_yet(name: str, step: int) -> int:
    typer.echo(f"'{name}' is not implemented yet (Step {step}).", err=True)
    return 1


if __name__ == "__main__":
    app()