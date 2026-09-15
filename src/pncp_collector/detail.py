"""Fase 2: detalhamento das capas ja persistidas (arquivos, itens e resultados).

Le as capas do banco, nunca da API. Quatro passos sequenciais, cada um com seu
loop, progresso e commit em lote, para que qualquer um possa ser retomado:

    A  resolver a compra e listar arquivos     (por documento)
    B  itens                                   (por compra)
    C  resultados                              (por item homologado com resultado)
    D  fechar o status dos documentos          (so banco)

As decisoes estao em detail_rules; aqui so se executa e se persiste.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.table import Table

from .client import DetailFetchError, DetailNotFound, PNCPClient
from .config import Settings, settings as default_settings
from .db import (
    CompraRow,
    DetailRepository,
    DocumentRow,
    build_engine,
    build_session_factory,
    create_schema,
)
from .detail_rules import (
    ATA,
    CONTRATO,
    DetailStats,
    decide_document_status,
    decide_items_status,
    has_target_file,
    homologated_items,
    items_needing_results,
    mark_winner,
    revisit_statuses,
    target_file_types,
)
from .ids import (
    CompraRef,
    parse_ata_key,
    parse_compra_key,
    parse_contrato_key,
    url_publica_compra,
)
from .models import DetailStatus, ItemsStatus, ResultsStatus
from .pipeline import BATCH_SIZE, make_progress
from .schemas import normalize_arquivo, normalize_item, normalize_resultado

logger = logging.getLogger(__name__)

DATASET = "detalhe"
STEPS = "ABCD"

# Documento nesses estados que volta a ter arquivo alvo obriga a compra a ser
# consultada de novo: o motivo de ter fechado pode ter mudado no PNCP.
RESET_COMPRA_FROM = {DetailStatus.SEM_HOMOLOGADO, DetailStatus.OK, DetailStatus.SEM_ARQUIVO}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


# --- resultados de cada chamada, produzidos nas threads e persistidos no principal ----


@dataclass
class DocumentOutcome:
    doc: DocumentRow
    compra_ref: CompraRef | None = None
    resolved_by: str | None = None
    arquivos: list[dict[str, Any]] = field(default_factory=list)
    has_file: bool | None = None
    error: str | None = None
    calls: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)


@dataclass
class CompraOutcome:
    compra: CompraRow
    itens: list[dict[str, Any]] = field(default_factory=list)
    not_found: bool = False
    error: str | None = None
    calls: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)


@dataclass
class ItemOutcome:
    numero_item: int
    resultados: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


class Detailer:
    def __init__(
        self,
        config: Settings | None = None,
        console: Console | None = None,
        repository_factory: Callable[[], AbstractContextManager[DetailRepository]] | None = None,
        client_factory: Callable[[Settings], Any] | None = None,
    ) -> None:
        self.config = config or default_settings
        self.console = console or Console()
        self.repository_factory = repository_factory or self._default_repository
        self.client_factory = client_factory or PNCPClient
        self._session_factory = None

    @contextmanager
    def _default_repository(self) -> Iterator[DetailRepository]:
        if self._session_factory is None:
            engine = build_engine(self.config)
            create_schema(engine)
            self._session_factory = build_session_factory(engine)
        with self._session_factory() as session:
            yield DetailRepository(session)

    # --- execucao ------------------------------------------------------------

    def run(
        self,
        tipo: str = "all",
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
        refresh: bool = False,
        revisit_sem_arquivo: bool = False,
        steps: str = STEPS,
    ) -> DetailStats:
        tipos = [ATA, CONTRATO] if tipo == "all" else [tipo]
        statuses = tuple(statuses or revisit_statuses(refresh, revisit_sem_arquivo))
        steps = steps.upper()
        stats = DetailStats()

        with self.repository_factory() as repo:
            run_id = repo.start_run(DATASET)
            for t, counts in repo.count_documents_by_status().items():
                for status, count in counts.items():
                    stats.documents_before[f"{t}:{status}"] = count

        with self.client_factory(self.config) as client, self.repository_factory() as repo:
            if "A" in steps:
                self._timed("A", stats, lambda: self.step_a(client, repo, tipos, statuses, limit, stats))
            if "B" in steps:
                self._timed("B", stats, lambda: self.step_b(client, repo, refresh, limit, stats))
            if "C" in steps:
                self._timed("C", stats, lambda: self.step_c(client, repo, refresh, limit, stats))
            if "D" in steps:
                self._timed("D", stats, lambda: self.step_d(repo, stats))

            for t, counts in repo.count_documents_by_status().items():
                for status, count in counts.items():
                    stats.documents_after[f"{t}:{status}"] = count
            repo.finish_run(run_id, stats.as_dict())

        return stats

    @staticmethod
    def _timed(step: str, stats: DetailStats, fn: Callable[[], None]) -> None:
        started = time.perf_counter()
        fn()
        stats.step_seconds[step] = round(time.perf_counter() - started, 1)

    def _map(self, fn: Callable[[Any], Any], items: Sequence[Any]) -> list[Any]:
        """Aplica fn a cada item, em paralelo se PNCP_DETAIL_CONCURRENCY > 1. Mantem a ordem."""
        if self.config.detail_concurrency <= 1 or len(items) <= 1:
            return [fn(item) for item in items]
        with ThreadPoolExecutor(max_workers=self.config.detail_concurrency) as pool:
            return list(pool.map(fn, items))

    # --- passo A: compra + arquivos, por documento ----------------------------------

    def _fetch_document(self, client: Any, doc: DocumentRow) -> DocumentOutcome:
        out = DocumentOutcome(doc=doc)
        targets = target_file_types(self.config, doc.tipo)

        if doc.tipo == ATA:
            ata_ref = parse_ata_key(doc.id)
            out.compra_ref = ata_ref.compra if ata_ref else None
            fetch_files = (lambda: client.get_arquivos_ata(ata_ref)) if ata_ref else None
            endpoint = "arquivos_ata"
        else:
            contrato_ref = parse_contrato_key(doc.id)
            out.compra_ref = parse_compra_key(doc.numero_controle_pncp_compra)
            out.resolved_by = "capa" if out.compra_ref else None
            if out.compra_ref is None and contrato_ref is not None:
                out.calls["contrato_detalhe"] += 1
                try:
                    payload = client.get_contrato(contrato_ref)
                    out.compra_ref = parse_compra_key(payload.get("numeroControlePncpCompra"))
                    out.resolved_by = "fallback" if out.compra_ref else None
                except DetailNotFound:
                    out.errors["contrato_detalhe"] += 1
                except DetailFetchError as exc:
                    out.errors["contrato_detalhe"] += 1
                    out.error = str(exc)
                    return out
            fetch_files = (lambda: client.get_arquivos_contrato(contrato_ref)) if contrato_ref else None
            endpoint = "arquivos_contrato"

        if out.compra_ref is None or fetch_files is None:
            return out  # sem_identificacao

        out.calls[endpoint] += 1
        try:
            raw_files = fetch_files()
        except DetailNotFound:
            raw_files = []
        except DetailFetchError as exc:
            out.errors[endpoint] += 1
            out.error = str(exc)
            return out

        out.arquivos = [normalize_arquivo(raw, doc.tipo, doc.id, targets) for raw in raw_files]
        out.has_file = has_target_file(out.arquivos, targets)
        return out

    def _persist_documents(
        self, repo: DetailRepository, tipo: str, outcomes: Sequence[DocumentOutcome], stats: DetailStats
    ) -> None:
        now = _now()
        compras: list[dict[str, Any]] = []
        resets: list[str] = []
        arquivos: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []

        for out in outcomes:
            stats.calls.update(out.calls)
            stats.errors.update(out.errors)
            stats.documents_processed += 1
            identified = out.compra_ref is not None
            status, error = decide_document_status(
                identified=identified, has_file=out.has_file, error=out.error
            )
            row: dict[str, Any] = {
                "id": out.doc.id,
                "detail_status": status,
                "detail_error": error,
                "detailed_at": now,
                "detail_attempts": out.doc.detail_attempts + 1,
            }
            if identified:
                key = out.compra_ref.key
                row["compra_key"] = key
                compras.append(
                    {
                        "compra_key": key,
                        "cnpj": out.compra_ref.cnpj,
                        "ano": out.compra_ref.ano,
                        "sequencial": out.compra_ref.sequencial,
                        "url_pncp": url_publica_compra(out.compra_ref),
                    }
                )
                if out.has_file and out.doc.detail_status in RESET_COMPRA_FROM:
                    resets.append(key)
            if tipo == CONTRATO:
                row["compra_resolved_by"] = out.resolved_by
            arquivos.extend(out.arquivos)
            updates.append(row)

        repo.ensure_compras(compras)
        repo.reset_compras(resets)
        stats.files_stored += repo.upsert_arquivos(arquivos)
        repo.update_documents(tipo, updates)
        repo.commit()

    def step_a(
        self,
        client: Any,
        repo: DetailRepository,
        tipos: Sequence[str],
        statuses: Sequence[str],
        limit: int | None,
        stats: DetailStats,
    ) -> None:
        for tipo in tipos:
            docs = repo.select_documents(
                tipo,
                statuses,
                max_attempts=self.config.detail_max_attempts,
                limit=limit,
                cnpj=self.config.cnpj,
            )
            with make_progress(self.console) as progress:
                task = progress.add_task(f"A arquivos ({tipo})", total=len(docs), detail="")
                for chunk in _chunks(docs, BATCH_SIZE):
                    outcomes = self._map(lambda d: self._fetch_document(client, d), chunk)
                    self._persist_documents(repo, tipo, outcomes, stats)
                    progress.update(task, advance=len(chunk), detail=f"erros {sum(stats.errors.values())}")

    # --- passo B: itens, por compra ---------------------------------------------------

    def _fetch_itens(self, client: Any, compra: CompraRow) -> CompraOutcome:
        out = CompraOutcome(compra=compra)
        ref = parse_compra_key(compra.compra_key)
        out.calls["itens"] += 1
        try:
            raw_items = client.get_itens(ref)
        except DetailNotFound:
            out.not_found = True
            return out
        except DetailFetchError as exc:
            out.errors["itens"] += 1
            out.error = str(exc)
            return out
        out.itens = [normalize_item(raw, compra.compra_key) for raw in raw_items]
        return out

    def _persist_compras(
        self, repo: DetailRepository, outcomes: Sequence[CompraOutcome], stats: DetailStats
    ) -> None:
        now = _now()
        for out in outcomes:
            stats.calls.update(out.calls)
            stats.errors.update(out.errors)
            stats.compras_fetched += 1
            attempts = out.compra.items_attempts + 1
            if out.error:
                repo.update_compra(
                    out.compra.compra_key,
                    items_status=ItemsStatus.ERRO,
                    items_error=out.error,
                    items_attempts=attempts,
                )
                continue
            stats.items_stored += repo.upsert_itens(out.itens)
            homologados = homologated_items(out.itens)
            repo.update_compra(
                out.compra.compra_key,
                items_status=decide_items_status(out.itens),
                items_error=None,
                items_fetched_at=now,
                items_attempts=attempts,
                total_itens=len(out.itens),
                total_homologados=len(homologados),
                # Sem item com resultado nao ha o que buscar no passo C.
                results_status=(
                    ResultsStatus.PENDENTE if items_needing_results(out.itens) else ResultsStatus.OK
                ),
                results_error=None,
            )
        repo.commit()

    def step_b(
        self, client: Any, repo: DetailRepository, refresh: bool, limit: int | None, stats: DetailStats
    ) -> None:
        statuses = [ItemsStatus.PENDENTE, ItemsStatus.ERRO]
        if refresh:
            statuses += [ItemsStatus.OK, ItemsStatus.SEM_ITENS]
        compras = repo.select_compras_for_items(
            statuses, max_attempts=self.config.detail_max_attempts, limit=limit
        )
        with make_progress(self.console) as progress:
            task = progress.add_task("B itens (compras)", total=len(compras), detail="")
            for chunk in _chunks(compras, BATCH_SIZE):
                outcomes = self._map(lambda c: self._fetch_itens(client, c), chunk)
                self._persist_compras(repo, outcomes, stats)
                progress.update(task, advance=len(chunk), detail=f"itens {stats.items_stored}")

    # --- passo C: resultados, por item homologado -------------------------------------

    def _fetch_resultados(self, client: Any, ref: CompraRef, numero_item: int) -> ItemOutcome:
        out = ItemOutcome(numero_item=numero_item)
        try:
            raw_results = client.get_resultados(ref, numero_item)
        except DetailFetchError as exc:
            out.error = str(exc)
            return out
        out.resultados = mark_winner(
            [
                normalize_resultado(raw, ref.key, numero_item, position)
                for position, raw in enumerate(raw_results)
            ]
        )
        return out

    def _process_compra_results(
        self, client: Any, repo: DetailRepository, compra: CompraRow, refresh: bool, stats: DetailStats
    ) -> None:
        ref = parse_compra_key(compra.compra_key)
        numeros = repo.select_items_for_results(compra.compra_key, refresh=refresh)
        outcomes: list[ItemOutcome] = self._map(
            lambda n: self._fetch_resultados(client, ref, n), numeros
        )
        stats.calls["resultados"] += len(outcomes)
        failed = [o for o in outcomes if o.error]
        stats.errors["resultados"] += len(failed)

        rows = [r for o in outcomes if not o.error for r in o.resultados]
        stats.results_stored += repo.upsert_resultados(rows)

        if not failed:
            status, error = ResultsStatus.OK, None
        elif len(failed) == len(outcomes):
            status, error = ResultsStatus.ERRO, failed[0].error
        else:
            status = ResultsStatus.PARCIAL
            error = f"{len(failed)} de {len(outcomes)} itens falharam; ultimo: {failed[-1].error}"
        repo.update_compra(
            compra.compra_key,
            results_status=status,
            results_error=error,
            results_fetched_at=_now(),
            results_attempts=compra.results_attempts + 1,
        )
        repo.commit()  # por compra: se cair no meio, a proxima execucao refaz so o que faltou

    def step_c(
        self, client: Any, repo: DetailRepository, refresh: bool, limit: int | None, stats: DetailStats
    ) -> None:
        compras = repo.select_compras_for_results(
            max_attempts=self.config.detail_max_attempts, limit=limit
        )
        with make_progress(self.console) as progress:
            task = progress.add_task("C resultados (compras)", total=len(compras), detail="")
            for compra in compras:
                self._process_compra_results(client, repo, compra, refresh, stats)
                progress.update(task, advance=1, detail=f"resultados {stats.results_stored}")

    # --- passo D: fechar status ---------------------------------------------------------

    def step_d(self, repo: DetailRepository, stats: DetailStats) -> None:
        now = _now()
        updates: dict[str, list[dict[str, Any]]] = {ATA: [], CONTRATO: []}
        for doc, compra in repo.documents_awaiting_close():
            if compra is None:
                status, error = DetailStatus.ERRO, "compra nao encontrada"
            else:
                status, error = decide_document_status(
                    identified=True,
                    has_file=True,
                    items_status=compra.items_status,
                    total_homologados=compra.total_homologados,
                    results_status=compra.results_status,
                    items_error=compra.items_error,
                )
            if status == DetailStatus.PENDENTE:
                continue
            updates[doc.tipo].append(
                {"id": doc.id, "detail_status": status, "detail_error": error, "detailed_at": now}
            )
        for tipo, rows in updates.items():
            repo.update_documents(tipo, rows)
            stats.documents_closed += len(rows)
        repo.commit()


def render_detail_summary(console: Console, stats: DetailStats) -> None:
    table = Table(title="Resumo - detalhamento")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    table.add_row("Documentos processados (passo A)", str(stats.documents_processed))
    table.add_row("Compras consultadas (passo B)", str(stats.compras_fetched))
    table.add_row("Arquivos gravados", str(stats.files_stored))
    table.add_row("Itens gravados", str(stats.items_stored))
    table.add_row("Resultados gravados", str(stats.results_stored))
    table.add_row("Documentos fechados (passo D)", str(stats.documents_closed))
    for endpoint, count in sorted(stats.calls.items()):
        table.add_row(f"    chamadas {endpoint}", str(count))
    for endpoint, count in sorted(stats.errors.items()):
        table.add_row(f"    erros {endpoint}", str(count))
    for step, seconds in stats.step_seconds.items():
        table.add_row(f"    passo {step}", f"{seconds:.1f}s")
    console.print(table)

    status_table = Table(title="Documentos por status (antes -> depois)")
    status_table.add_column("Tipo:status")
    status_table.add_column("Antes", justify="right")
    status_table.add_column("Depois", justify="right")
    for key in sorted(set(stats.documents_before) | set(stats.documents_after)):
        status_table.add_row(
            key, str(stats.documents_before.get(key, 0)), str(stats.documents_after.get(key, 0))
        )
    console.print(status_table)
