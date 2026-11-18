"""Interface de linha de comando do coletor."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
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
from .detail_rules import parse_interest_threshold
from .item_classifier import ItemClassifier, render_predict_summary, render_train_summary
from .item_labeling import SLICES, ItemLabeler, render_label_summary
from .models import LabelStatus, TriageStatus
from .pipeline import Collector, render_summary
from .triage import Triager, render_triage_summary

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
    triagem: Annotated[
        str | None,
        typer.Option(
            "--triagem",
            help="So capas cuja compra esta nesses triage_status (ex.: potencial ou "
            "potencial,indefinido). Padrao: todas, como antes da fase 3.",
        ),
    ] = None,
    todos_os_itens: Annotated[
        bool,
        typer.Option(
            "--todos-os-itens",
            help="Desliga o filtro do classificador: passo C para todo item homologado de "
            "compra com documento pendente (milhoes de chamadas). Padrao: so itens com "
            "predicted_interest >= limiar guardado no banco (comando `limiar`), e passo A "
            "so nas capas dessas compras.",
        ),
    ] = False,
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
    triage_statuses = _parse_triage_statuses(triagem)
    detailer = Detailer(settings, console)
    stats = detailer.run(
        tipo=tipo,
        statuses=statuses,
        limit=limit,
        refresh=refresh,
        revisit_sem_arquivo=revisit_sem_arquivo,
        steps=steps,
        triage_statuses=triage_statuses,
        min_interest=None if todos_os_itens else _interest_threshold(),
    )
    render_detail_summary(console, stats)


@contextmanager
def _repository() -> Iterator[DetailRepository]:
    """Sessao com o esquema garantido (cria tabela nova, como `settings`)."""
    engine = build_engine(settings)
    create_schema(engine)
    with build_session_factory(engine)() as session:
        yield DetailRepository(session)


def _interest_threshold() -> float:
    """Limiar de interesse em vigor: o do banco; o do `.env` so semeia a primeira vez."""
    with _repository() as repo:
        return repo.interest_threshold(settings.item_interest_threshold)


def _render_interest(title: str, counts: dict[str, int]) -> None:
    table = Table(title=title)
    table.add_column("Itens de interesse")
    table.add_column("Quantidade", justify="right")
    table.add_row("resultado ja buscado", str(counts["items_fetched"]))
    table.add_row("a buscar", str(counts["items_pending"]))
    table.add_row("    em compras", str(counts["compras_pending"]))
    console.print(table)


@app.command()
def limiar(
    valor: Annotated[
        str | None,
        typer.Argument(help="Novo limiar, entre 0 e 1 (ex.: 0.4). Sem valor, so mostra o atual."),
    ] = None,
) -> None:
    """Mostra ou muda o limiar de interesse do passo C, guardado no banco.

    O item entra no passo C quando `predicted_interest` (soma das chances das
    categorias de interesse) e maior ou igual ao limiar. Baixar o limiar so
    acrescenta itens a buscar; subir nao apaga resultado ja gravado.
    """
    with _repository() as repo:
        current = repo.interest_threshold(settings.item_interest_threshold)
        if valor is None:
            row = repo.interest_threshold_row()
            console.print(
                f"Limiar de interesse: [bold]{current:g}[/bold] "
                f"(no banco desde {row.updated_at:%Y-%m-%d %H:%M})"
            )
            _render_interest(f"Passo C com limiar {current:g}", repo.count_interest_results(current))
            return
        try:
            new = parse_interest_threshold(valor)
        except ValueError as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=2) from None
        before = repo.count_interest_results(current)
        repo.set_interest_threshold(new)
        after = repo.count_interest_results(new)
        console.print(f"Limiar de interesse: {current:g} -> [bold]{new:g}[/bold]")
        _render_interest(f"Antes (limiar {current:g})", before)
        _render_interest(f"Depois (limiar {new:g})", after)


TRIAGE_STATUSES = {
    TriageStatus.PENDENTE,
    TriageStatus.POTENCIAL,
    TriageStatus.FORA_POR_ORA,
    TriageStatus.INDEFINIDO,
    TriageStatus.ERRO,
}


def _parse_triage_statuses(value: str | None) -> list[str] | None:
    if not value:
        return None
    parts = [part.strip() for part in value.split(",") if part.strip()]
    invalid = [part for part in parts if part not in TRIAGE_STATUSES]
    if invalid:
        console.print(
            f"[red]triage_status invalido: {', '.join(invalid)}. "
            f"Use {', '.join(sorted(TRIAGE_STATUSES))}.[/red]"
        )
        raise typer.Exit(code=2)
    return parts


@app.command()
def triar(
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="triage_status a (re)triar, separados por virgula. Padrao: pendente,erro. "
            "Use fora_por_ora,indefinido,potencial para reclassificar com outro modelo.",
        ),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Maximo de compras nesta execucao.")
    ] = None,
    no_seed: Annotated[
        bool, typer.Option("--no-seed", help="Nao cria compras a partir das capas antes.")
    ] = False,
    only_seed: Annotated[
        bool, typer.Option("--only-seed", help="So cria as compras; nao chama o modelo.")
    ] = False,
    origem: Annotated[
        str | None,
        typer.Option(
            "--origem",
            help="So compras cujo objeto veio desse tipo de capa: ata ou contrato. "
            "Padrao: todas. Comece por `ata`: e a fonte principal e custa ~1/3.",
        ),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fase 3: triagem das compras pelo objeto da capa (modelo de decisoes)."""
    _setup_logging(verbose)
    if origem not in (None, "ata", "contrato"):
        console.print("[red]--origem deve ser ata ou contrato.[/red]")
        raise typer.Exit(code=2)
    if not only_seed and not settings.openrouter_api_key:
        console.print("[red]Defina PNCP_OPENROUTER_API_KEY (.env) para chamar o modelo.[/red]")
        raise typer.Exit(code=2)
    statuses = _parse_triage_statuses(status)
    triager = Triager(settings, console)
    stats = triager.run(
        statuses=statuses, limit=limit, seed=not no_seed, triage=not only_seed, source=origem
    )
    render_triage_summary(console, stats, settings)


@app.command("triar-itens")
def triar_itens(
    status: Annotated[
        str | None,
        typer.Option(
            "--status",
            help="label_status a (re)rotular, separados por virgula. Padrao: pendente,erro.",
        ),
    ] = None,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Maximo de descricoes nesta execucao.")
    ] = None,
    fatia: Annotated[
        str | None,
        typer.Option("--fatia", help="So uma fatia da amostra: peneira, reserva, aleatorio ou ativo."),
    ] = None,
    sample_reserva: Annotated[
        int, typer.Option("--sample-reserva", help="Tamanho alvo da fatia reserva.")
    ] = 10_000,
    sample_aleatorio: Annotated[
        int, typer.Option("--sample-aleatorio", help="Tamanho alvo da fatia aleatorio.")
    ] = 40_000,
    no_seed: Annotated[
        bool, typer.Option("--no-seed", help="Nao semeia item_labels a partir de itens antes.")
    ] = False,
    only_seed: Annotated[
        bool, typer.Option("--only-seed", help="So semeia; nao chama o modelo.")
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fase 4: rotula descricoes distintas de item (modelo de decisoes) para o treino.

    Semeia `item_labels` com a peneira inteira mais as fatias reserva e aleatorio, e
    pede ao modelo a categoria de cada descricao pendente. Teto de gasto:
    PNCP_TRIAGE_MAX_COST_USD.
    """
    _setup_logging(verbose)
    if fatia is not None and fatia not in SLICES:
        console.print(f"[red]--fatia deve ser uma de {', '.join(SLICES)}.[/red]")
        raise typer.Exit(code=2)
    if not only_seed and not settings.openrouter_api_key:
        console.print("[red]Defina PNCP_OPENROUTER_API_KEY (.env) para chamar o modelo.[/red]")
        raise typer.Exit(code=2)
    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        known = {LabelStatus.PENDENTE, LabelStatus.OK, LabelStatus.ERRO}
        bad = [s for s in statuses if s not in known]
        if bad:
            console.print(f"[red]label_status desconhecido: {', '.join(bad)}.[/red]")
            raise typer.Exit(code=2)
    labeler = ItemLabeler(settings, console)
    stats = labeler.run(
        statuses=statuses,
        limit=limit,
        seed=not no_seed,
        label=not only_seed,
        sample_slice=fatia,
        sample_reserva=sample_reserva,
        sample_aleatorio=sample_aleatorio,
    )
    render_label_summary(console, stats)


@app.command("treinar-itens")
def treinar_itens(
    class_weight: Annotated[
        str | None,
        typer.Option(
            "--class-weight",
            help="balanced (favorece recall, padrao) ou none (favorece precisao). "
            "Padrao: PNCP_ITEM_CLASS_WEIGHT.",
        ),
    ] = None,
    holdout: Annotated[
        float, typer.Option("--holdout", help="Fracao separada para medir (padrao 0,1).")
    ] = 0.1,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fase 5: treina o classificador local de itens com os rotulos de item_labels.

    Exclui o gabarito humano (PNCP_ITEM_VALIDATION_CSV), mede no holdout e no gabarito e
    salva o modelo em PNCP_MODELS_DIR (fora do git). So banco e disco.
    """
    _setup_logging(verbose)
    if class_weight not in (None, "balanced", "none"):
        console.print("[red]--class-weight deve ser balanced ou none.[/red]")
        raise typer.Exit(code=2)
    stats = ItemClassifier(settings, console).train(holdout=holdout, class_weight=class_weight)
    render_train_summary(console, stats)


@app.command("prever-itens")
def prever_itens(
    refresh: Annotated[
        bool, typer.Option("--refresh", help="Refaz tambem itens ja previstos por este modelo.")
    ] = False,
    limit: Annotated[
        int | None, typer.Option("--limit", help="Maximo de compras nesta execucao.")
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Fase 5: grava em `itens` a categoria prevista, a confianca e o interesse de cada item.

    Retomavel: sem --refresh, so itens ainda sem previsao do modelo atual.
    """
    _setup_logging(verbose)
    classifier = ItemClassifier(settings, console)
    try:
        summary = classifier.predict(refresh=refresh, limit=limit)
    except FileNotFoundError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    render_predict_summary(console, summary)


@app.command()
def status() -> None:
    """Quanto falta: documentos por detail_status e compras por items/results_status."""
    with _repository() as repo:
        documents = repo.count_documents_by_status()
        compras = repo.count_compras_by_status()
        triage = repo.count_compras_by_triage()
        threshold = repo.interest_threshold(settings.item_interest_threshold)
        interest = repo.count_interest_results(threshold)

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

    _render_interest(
        f"Passo C filtrado (interesse >= {threshold:g}, homologado com resultado)", interest
    )

    table = Table(title="Compras por triage_status (fase 3)")
    table.add_column("Status")
    table.add_column("Compras", justify="right")
    for key in sorted(triage):
        table.add_row(key, str(triage[key]))
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
    """Coleta atas, depois contratos (a ordem importa para o dedup) e detalha.

    Nao tria: a fase 3 e `triar`, e o `detalhar` recortado e `detalhar --triagem potencial`.
    """
    _setup_logging(verbose)
    collector = Collector(settings, console)
    today = _reference(reference, collector, ["atas", "contratos"])
    render_summary(console, "atas", collector.collect_atas(today))
    render_summary(console, "contratos", collector.collect_contratos(today))
    if not skip_detail:
        render_detail_summary(console, Detailer(settings, console).run())


if __name__ == "__main__":
    app()
