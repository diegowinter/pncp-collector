"""Passo B (itens) para todas as compras, escolhidas pela triagem de capa, sem exigir o passo A.

E o passo B da fase 2 rodando fora do `detalhar`, que so toca compras com documento ja
passado pelo passo A (arquivos). Aqui a selecao e pela tabela `compras` (semeada pelas
capas em `triar --only-seed`), e a gravacao e a mesma do `detalhar`
(Detailer._fetch_itens e Detailer._persist_compras): tabela `itens`, mais `items_status`,
`total_itens`, `results_status` em `compras`. Nada de formato proprio.

Nasceu como `itens_treino.py`, para montar o conjunto de treino do classificador de itens;
virou o passo B oficial quando ficou claro que baixar itens de tudo e barato (56 mil compras
em 3 h, so API) e que o corte certo e pelo item, nao pela capa.

Selecao, nesta ordem (compras cujo objeto veio do tipo de capa em --origem, padrao ata):

    1. triage_status = potencial      (todas)
    2. triage_status = indefinido     (todas)
    3. triage_status = fora_por_ora   (todas; ou so uma amostra de --sample-fora compras)
    4. triage_status = pendente       (todas; e o caso dos contratos, que nao passam pelo Jev)

Com --sample-fora N, a amostra de fora_por_ora e estavel entre execucoes: as N primeiras
compras na ordem de md5(compra_key), entao aumentar N so acrescenta no fim. Foi assim que
o treino usou 30 mil. So compras com items_status pendente ou erro (abaixo de
PNCP_DETAIL_MAX_ATTEMPTS) sao buscadas.

    uv run python scripts/baixar_itens.py --dry-run                 # so conta
    uv run python scripts/baixar_itens.py --limit 20                # teste
    uv run python scripts/baixar_itens.py                           # todas as compras de ata
    uv run python scripts/baixar_itens.py --origem contrato         # todas as compras de contrato

Banco e ritmo vem do .env (PNCP_DATABASE_URL, PNCP_DETAIL_CONCURRENCY,
PNCP_REQUEST_DELAY). Ctrl+C encerra na hora; rodar de novo retoma.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from pncp_collector.config import settings  # noqa: E402
from pncp_collector.db import CompraRow, DetailRepository, _compra_row  # noqa: E402
from pncp_collector.detail import Detailer, _chunks  # noqa: E402
from pncp_collector.detail_rules import DetailStats  # noqa: E402
from pncp_collector.models import Compra, ItemsStatus, TriageStatus  # noqa: E402
from pncp_collector.pipeline import BATCH_SIZE, make_progress  # noqa: E402

DATASET = "itens_treino"  # nome historico nos collection_runs; mantido para nao quebrar a serie
ORDEM = (
    TriageStatus.POTENCIAL,
    TriageStatus.INDEFINIDO,
    TriageStatus.FORA_POR_ORA,
    TriageStatus.PENDENTE,
)
RETRY_STATUSES = (ItemsStatus.PENDENTE, ItemsStatus.ERRO)

console = Console()


def _pending(compra: Compra, max_attempts: int | None) -> bool:
    if compra.items_status not in RETRY_STATUSES:
        return False
    if (
        max_attempts is not None
        and compra.items_status == ItemsStatus.ERRO
        and (compra.items_attempts or 0) >= max_attempts
    ):
        return False
    return True


def select_training_compras(
    repo: DetailRepository, *, sample_fora: int | None, max_attempts: int | None, origem: str = "ata"
) -> dict[str, tuple[int, list[CompraRow]]]:
    """Por status da triagem: (tamanho do universo, compras ainda por buscar)."""
    out: dict[str, tuple[int, list[CompraRow]]] = {}
    for status in ORDEM:
        query = select(Compra).where(
            Compra.objeto_source == origem, Compra.triage_status == status
        )
        if status == TriageStatus.FORA_POR_ORA and sample_fora:
            # Amostra estavel: md5 embaralha, LIMIT corta sempre no mesmo lugar,
            # independentemente de quantas ja foram buscadas.
            query = query.order_by(func.md5(Compra.compra_key)).limit(sample_fora)
        else:
            query = query.order_by(Compra.compra_key)
        universe = list(repo.session.scalars(query))
        todo = [_compra_row(c) for c in universe if _pending(c, max_attempts)]
        out[status] = (len(universe), todo)
    return out


def run(detailer: Detailer, selection: dict[str, tuple[int, list[CompraRow]]], limit: int | None) -> DetailStats:
    stats = DetailStats()
    remaining = limit
    with detailer.client_factory(detailer.config) as client, detailer.repository_factory() as repo:
        run_id = repo.start_run(DATASET)
        for status in ORDEM:
            _, compras = selection[status]
            if remaining is not None:
                compras = compras[:remaining]
                remaining -= len(compras)
            if not compras:
                continue
            with make_progress(console) as progress:
                task = progress.add_task(f"B itens ({status})", total=len(compras), detail="")
                for chunk in _chunks(compras, BATCH_SIZE):
                    outcomes = detailer._map(lambda c: detailer._fetch_itens(client, c), chunk)
                    _persist_resiliente(detailer, repo, outcomes, stats)
                    progress.update(
                        task,
                        advance=len(chunk),
                        detail=f"itens {stats.items_stored} erros {sum(stats.errors.values())}",
                    )
        repo.finish_run(run_id, stats.as_dict())
    return stats


def _persist_resiliente(
    detailer: Detailer, repo: DetailRepository, outcomes: Sequence, stats: DetailStats
) -> None:
    """Grava o lote; se o banco recusar, desfaz e grava compra a compra, marcando a que
    falhou como `erro` com uma mensagem curta. O SQLAlchemy despeja todos os parametros
    do comando na excecao (milhares de itens), entao o que vai para o console e para
    `items_error` e so a primeira linha."""
    try:
        detailer._persist_compras(repo, outcomes, stats)
        return
    except Exception as exc:  # noqa: BLE001 - qualquer falha de banco cai aqui
        repo.session.rollback()
        console.print(f"[yellow]Lote recusado pelo banco ({_resumo(exc)}); regravando compra a compra.[/yellow]")
    for out in outcomes:
        try:
            detailer._persist_compras(repo, [out], stats)
        except Exception as exc:  # noqa: BLE001
            repo.session.rollback()
            resumo = _resumo(exc)
            console.print(f"[red]Compra {out.compra.compra_key} nao gravada: {resumo}[/red]")
            repo.update_compra(
                out.compra.compra_key,
                items_status=ItemsStatus.ERRO,
                items_error=f"gravacao: {resumo}",
                items_attempts=out.compra.items_attempts + 1,
            )
            repo.commit()
            stats.errors["gravacao"] += 1


def _resumo(exc: Exception) -> str:
    """Tipo e primeira linha da mensagem, sem o despejo de parametros."""
    first = str(exc).splitlines()[0] if str(exc) else ""
    return f"{type(exc).__name__}: {first[:300]}"


def print_selection(selection: dict[str, tuple[int, list[CompraRow]]], origem: str) -> None:
    table = Table(title=f"Compras ({origem}) por triagem")
    table.add_column("triage_status")
    table.add_column("universo", justify="right")
    table.add_column("por buscar", justify="right")
    for status in ORDEM:
        universe, todo = selection[status]
        table.add_row(status, str(universe), str(len(todo)))
    console.print(table)


def print_stats(stats: DetailStats) -> None:
    table = Table(title="Passo B (baixar_itens)")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    table.add_row("Compras consultadas", str(stats.compras_fetched))
    table.add_row("Itens gravados", str(stats.items_stored))
    for name, n in sorted(stats.calls.items()):
        table.add_row(f"Chamadas {name}", str(n))
    for name, n in sorted(stats.errors.items()):
        table.add_row(f"Erros {name}", str(n))
    console.print(table)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample-fora", type=int, default=None, help="so uma amostra de N compras fora_por_ora (padrao: todas)")
    parser.add_argument("--limit", type=int, default=None, help="maximo de compras nesta execucao (teste)")
    parser.add_argument("--origem", choices=("ata", "contrato"), default="ata", help="tipo de capa de onde veio o objeto da compra (padrao ata)")
    parser.add_argument("--dry-run", action="store_true", help="so mostra a selecao, nao chama a API")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )

    detailer = Detailer(settings, console)
    with detailer.repository_factory() as repo:
        selection = select_training_compras(
            repo,
            sample_fora=args.sample_fora,
            max_attempts=settings.detail_max_attempts,
            origem=args.origem,
        )
    print_selection(selection, args.origem)
    if args.dry_run:
        return
    try:
        stats = run(detailer, selection, args.limit)
    except KeyboardInterrupt:
        console.print("[yellow]Interrompido; rode de novo para retomar.[/yellow]")
        return
    print_stats(stats)


if __name__ == "__main__":
    main()
