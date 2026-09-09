"""Interface de linha de comando do coletor."""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler

from .config import settings
from .db import build_engine, create_schema, drop_schema
from .pipeline import Collector, render_summary

app = typer.Typer(help="Coletor de atas e contratos do PNCP.", no_args_is_help=True)
console = Console()


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


@app.command("init-db")
def init_db() -> None:
    """Cria as tabelas no Postgres."""
    create_schema(build_engine(settings))
    console.print("[green]Schema criado.[/green]")


@app.command("reset-db")
def reset_db(
    yes: Annotated[bool, typer.Option("--yes", help="Confirma a exclusao.")] = False,
) -> None:
    """Remove e recria as tabelas. Destrutivo."""
    if not yes:
        console.print("[red]Use --yes para confirmar: isso apaga todos os dados.[/red]")
        raise typer.Exit(code=1)
    engine = build_engine(settings)
    drop_schema(engine)
    create_schema(engine)
    console.print("[green]Schema recriado.[/green]")


@app.command()
def atas(
    reference: Annotated[
        str | None,
        typer.Option("--reference", help="Data de referencia (yyyy-MM-dd). Padrao: hoje."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Coleta atas de registro de precos (janela por vigencia)."""
    _setup_logging(verbose)
    today = date.fromisoformat(reference) if reference else date.today()
    collector = Collector(settings, console)
    render_summary(console, "atas", collector.collect_atas(today))


@app.command()
def contratos(
    reference: Annotated[
        str | None,
        typer.Option("--reference", help="Data de referencia (yyyy-MM-dd). Padrao: hoje."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Coleta contratos (janela por publicacao)."""
    _setup_logging(verbose)
    today = date.fromisoformat(reference) if reference else date.today()
    collector = Collector(settings, console)
    render_summary(console, "contratos", collector.collect_contratos(today))


@app.command()
def all(
    reference: Annotated[
        str | None,
        typer.Option("--reference", help="Data de referencia (yyyy-MM-dd). Padrao: hoje."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Coleta atas e, em seguida, contratos (a ordem importa para o dedup)."""
    _setup_logging(verbose)
    today = date.fromisoformat(reference) if reference else date.today()
    collector = Collector(settings, console)
    render_summary(console, "atas", collector.collect_atas(today))
    render_summary(console, "contratos", collector.collect_contratos(today))


if __name__ == "__main__":
    app()
