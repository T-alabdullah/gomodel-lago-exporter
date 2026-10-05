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


@app.command()
def run() -> None:
    """Run the export loop. (Step 8)"""
    raise typer.Exit(_not_yet("run", 8))


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