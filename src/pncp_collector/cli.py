"""Interface de linha de comando do coletor."""

from __future__ import annotations

import logging
from datetime import date
from typing import Annotated

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from .config import settings
from .db import (
    DetailRepository,
    build_engine,
    build_session_factory,
    create_schema,
    drop_schema,
)
from .detail import STEPS, Detailer, render_detail_summary
from .pipeline import Collector, render_summary

REFERENCE_HELP = (
    "Data de referencia (yyyy-MM-dd). Padrao: retoma a ultima varredura incompleta; "
    "se nao houver, hoje."
)

app = typer.Typer(help="Coletor de atas e contratos do PNCP.", no_args_is_help=True)
console = Console()


def _reference(reference: str | None, collector: Collector, datasets: list[str]) -> date:
    if reference:
        return date.fromisoformat(reference)
    pending = collector.pending_reference(datasets)
    if pending is not None:
        console.print(
            f"[yellow]Varredura incompleta com referencia {pending}: retomando. "
            "Use --reference para comecar outra.[/yellow]"
        )
        return pending
    return date.today()


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
        typer.Option("--reference", help=REFERENCE_HELP),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Coleta atas de registro de precos (janela por vigencia)."""
    _setup_logging(verbose)
    collector = Collector(settings, console)
    today = _reference(reference, collector, ["atas"])
    render_summary(console, "atas", collector.collect_atas(today))


@app.command()
def contratos(
    reference: Annotated[
        str | None,
        typer.Option("--reference", help=REFERENCE_HELP),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Coleta contratos (janela por publicacao)."""
    _setup_logging(verbose)
    collector = Collector(settings, console)
    today = _reference(reference, collector, ["contratos"])
    render_summary(console, "contratos", collector.collect_contratos(today))


@app.command()
def detalhar(
    tipo: Annotated[
        str, typer.Option("--tipo", help="ata, contrato ou all.")
    ] = "all",
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="Lista de detail_status a revisitar (ex.: pendente,erro). "
            "Padrao: pendente, erro e sem_homologado.",
        ),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Maximo de documentos/compras por passo.")
    ] = None,
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Revisita tambem documentos ok e refaz resultados.")
    ] = False,
    revisit_sem_arquivo: Annotated[
        bool, typer.Option("--revisit-sem-arquivo", help="Revisita documentos sem_arquivo.")
    ] = False,
    only_step: Annotated[
        str | None,
        typer.Option("--only-step", help="Executa so os passos indicados (ex.: A, BC, D)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fase 2: arquivos, itens e resultados das capas ja persistidas."""
    _setup_logging(verbose)
    if tipo not in ("ata", "contrato", "all"):
        console.print("[red]--tipo deve ser ata, contrato ou all.[/red]")
        raise typer.Exit(code=2)
    steps = (only_step or STEPS).upper()
    if any(step not in STEPS for step in steps):
        console.print(f"[red]--only-step aceita so letras de {STEPS}.[/red]")
        raise typer.Exit(code=2)
    statuses = [part.strip() for part in status.split(",") if part.strip()] if status else None
    detailer = Detailer(settings, console)
    stats = detailer.run(
        tipo=tipo,
        statuses=statuses,
        limit=limit,
        refresh=refresh,
        revisit_sem_arquivo=revisit_sem_arquivo,
        steps=steps,
    )
    render_detail_summary(console, stats)


@app.command()
def status() -> None:
    """Quanto falta: documentos por detail_status e compras por items/results_status."""
    with build_session_factory(build_engine(settings))() as session:
        repo = DetailRepository(session)
        documents = repo.count_documents_by_status()
        compras = repo.count_compras_by_status()

    table = Table(title="Documentos por detail_status")
    table.add_column("Status")
    table.add_column("Atas", justify="right")
    table.add_column("Contratos", justify="right")
    for key in sorted(set(documents.get("ata", {})) | set(documents.get("contrato", {}))):
        table.add_row(
            key,
            str(documents.get("ata", {}).get(key, 0)),
            str(documents.get("contrato", {}).get(key, 0)),
        )
    console.print(table)

    table = Table(title="Compras")
    table.add_column("Status")
    table.add_column("items_status", justify="right")
    table.add_column("results_status", justify="right")
    for key in sorted(set(compras["items"]) | set(compras["results"])):
        table.add_row(key, str(compras["items"].get(key, 0)), str(compras["results"].get(key, 0)))
    console.print(table)


@app.command()
def all(
    reference: Annotated[
        str | None,
        typer.Option("--reference", help=REFERENCE_HELP),
    ] = None,
    skip_detail: Annotated[
        bool, typer.Option("--skip-detail", help="So a fase 1 (atas e contratos).")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Coleta atas, depois contratos (a ordem importa para o dedup) e detalha."""
    _setup_logging(verbose)
    collector = Collector(settings, console)
    today = _reference(reference, collector, ["atas", "contratos"])
    render_summary(console, "atas", collector.collect_atas(today))
    render_summary(console, "contratos", collector.collect_contratos(today))
    if not skip_detail:
        render_detail_summary(console, Detailer(settings, console).run())


if __name__ == "__main__":
    app()
