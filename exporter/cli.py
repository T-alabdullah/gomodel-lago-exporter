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