"""Orquestracao da varredura dia a dia, com progresso em rich."""

from __future__ import annotations

import logging
import threading
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from itertools import islice
from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.table import Table
from sqlalchemy.orm import Session, sessionmaker

from .client import DayFetchError, PNCPClient
from .config import Settings, settings as default_settings
from .db import (
    build_engine,
    build_session_factory,
    create_schema,
    load_ata_keys,
    upsert,
)
from .filters import (
    ATA_FIELDS,
    CONTRATO_FIELDS,
    DatasetFields,
    DiscardReason,
    FilterStats,
    check_record,
)
from .models import Ata, CollectionRun, Contrato
from .schemas import normalize_ata, normalize_contrato

logger = logging.getLogger(__name__)

BATCH_SIZE = 500


def date_range(start: date, end: date) -> list[date]:
    """Dias de start ate end, inclusive nas duas pontas."""
    if end < start:
        return []
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


def make_progress(console: Console) -> Progress:
    return Progress(
        SpinnerColumn(),
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("{task.fields[detail]}"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
    )


class _PageTracker:
    """Alimenta a barra auxiliar de paginas a partir de varias threads.

    Quantas paginas a varredura tem no total so se sabe depois de buscar a
    primeira pagina de cada dia — `totalPaginas` vem no corpo da resposta. Por
    isso o total da barra cresce conforme os dias comecam, em vez de ser fixo:
    e um total parcial, honesto sobre o que ja se conhece.
    """

    def __init__(self, progress: Progress, task_id: TaskID) -> None:
        self._progress = progress
        self._task_id = task_id
        self._lock = threading.Lock()
        self._fetched = 0
        self._known = 0
        self._days_seen: set[date] = set()

    def __call__(self, day: date, page: int, total_pages: int) -> None:
        with self._lock:
            if day not in self._days_seen:
                self._days_seen.add(day)
                self._known += total_pages
            self._fetched += 1
            self._progress.update(
                self._task_id,
                completed=self._fetched,
                total=self._known,
                detail=f"{len(self._days_seen)} dias conhecidos",
            )


class Collector:
    def __init__(
        self,
        config: Settings | None = None,
        console: Console | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.config = config or default_settings
        self.console = console or Console()
        if session_factory is None:
            engine = build_engine(self.config)
            create_schema(engine)
            session_factory = build_session_factory(engine)
        self.session_factory = session_factory

    # --- janelas de varredura ------------------------------------------------

    def ata_days(self, today: date) -> list[date]:
        """Vigencia: retroativo de 364 dias mais 90 dias a frente (assinadas)."""
        past = date_range(today - timedelta(days=self.config.atas_days_back), today)
        future = date_range(
            today + timedelta(days=1),
            today + timedelta(days=self.config.atas_days_ahead),
        )
        return past + future

    def contrato_days(self, today: date) -> list[date]:
        """Publicacao: retroativo de 400 dias."""
        return date_range(today - timedelta(days=self.config.contratos_days_back), today)

    # --- execucao ------------------------------------------------------------

    def collect_atas(self, today: date | None = None) -> FilterStats:
        today = today or date.today()

        def fetch(client: PNCPClient, day: date) -> Iterator[dict[str, Any]]:
            return client.iter_atas(day)

        return self._run(
            dataset="atas",
            days=self.ata_days(today),
            today=today,
            fetch=fetch,
            normalize=normalize_ata,
            model=Ata,
            fields=ATA_FIELDS,
            # Dedup obrigatorio: a mesma ata volta em todo dia que a vigencia cobre.
            seen=set(),
        )

    def collect_contratos(self, today: date | None = None) -> FilterStats:
        today = today or date.today()

        with self.session_factory() as session:
            ata_keys = load_ata_keys(session)

        def fetch(client: PNCPClient, day: date) -> Iterator[dict[str, Any]]:
            return client.iter_contratos(day)

        return self._run(
            dataset="contratos",
            days=self.contrato_days(today),
            today=today,
            fetch=fetch,
            normalize=normalize_contrato,
            model=Contrato,
            fields=CONTRATO_FIELDS,
            # Sem dedup entre dias: cada contrato aparece so no dia da publicacao.
            # O set ainda protege contra repeticao dentro da mesma execucao.
            seen=set(),
            ata_keys=ata_keys,
        )

    @staticmethod
    def _derivation_match(
        record: dict[str, Any], ata_keys: tuple[set[str], set[str]]
    ) -> str | None:
        """Como o contrato se liga a uma ata ja coletada, se e que se liga.

        "ata" e o link explicito (numeroControlePncpAta); "compra" e o fallback
        para quando esse campo nao foi preenchido, mas o contrato saiu da mesma
        compra de uma ata da base.
        """
        ata_ids, compra_ids = ata_keys
        ata_ref = record.get("numero_controle_pncp_ata")
        if ata_ref and ata_ref in ata_ids:
            return "ata"
        compra = record.get("numero_controle_pncp_compra")
        if compra and compra in compra_ids:
            return "compra"
        return None

    def _handle_record(
        self,
        raw: dict[str, Any],
        normalize: Callable[[dict[str, Any]], dict[str, Any]],
        model: type[Ata] | type[Contrato],
        fields: DatasetFields,
        today: date,
        seen: set[str],
        ata_keys: tuple[set[str], set[str]] | None,
        stats: FilterStats,
        buffer: list[dict[str, Any]],
        session: Session,
    ) -> None:
        """Aplica dedup e filtros locais e enfileira o registro para gravacao."""
        stats.fetched += 1
        record = normalize(raw)

        identifier = record.get(fields.identifier)
        if identifier and identifier in seen:
            stats.duplicates += 1
            return
        if identifier:
            seen.add(identifier)

        discard, suspicious_reasons = check_record(record, fields, self.config, today)

        if discard is None and ata_keys is not None:
            match = self._derivation_match(record, ata_keys)
            record["derived_from_ata"] = match is not None
            record["derivation_match"] = match
            if match is not None:
                stats.derived_from_ata += 1
                # O preco ja esta registrado pela ata de origem. Por padrao o
                # contrato e mantido e apenas marcado; o descarte e opcional.
                if self.config.discard_derived_contracts:
                    discard = DiscardReason.DERIVED_FROM_ATA

        if discard:
            stats.discarded[discard] += 1
            return

        record["suspicious"] = bool(suspicious_reasons)
        record["suspicious_reasons"] = suspicious_reasons or None
        if suspicious_reasons:
            stats.suspicious += 1

        buffer.append(record)
        stats.kept += 1

        if len(buffer) >= BATCH_SIZE:
            upsert(session, model, buffer)
            session.commit()
            buffer.clear()

    def _iter_days(
        self,
        client: PNCPClient,
        days: list[date],
        fetch: Callable[[PNCPClient, date], Iterator[dict[str, Any]]],
    ) -> Iterator[tuple[date, Callable[[], Iterable[dict[str, Any]]]]]:
        """Rende (dia, pegar_registros) na ordem de `days`, com ate
        PNCP_LIST_CONCURRENCY dias sendo buscados em paralelo.

        A entrega e sempre na ordem de `days`, nunca na ordem em que a API
        respondeu: dedup, gravacao e estatisticas seguem deterministicos, e o
        banco continua sendo tocado so pela thread principal. Chamar
        `pegar_registros()` devolve a lista do dia ou levanta DayFetchError.

        Em paralelo, um dia inteiro fica em memoria por worker — a janela e
        pequena de proposito por isso. Com concorrencia 1 nada muda: os
        registros seguem sendo consumidos pagina a pagina, sem materializar.
        """
        workers = min(self.config.list_concurrency, len(days))
        if workers <= 1:
            for day in days:
                yield day, lambda d=day: fetch(client, d)
            return

        with ThreadPoolExecutor(max_workers=workers) as pool:
            upcoming = iter(days)
            pending: deque[Future[list[dict[str, Any]]]] = deque(
                pool.submit(lambda d=day: list(fetch(client, d)))
                for day in islice(upcoming, workers)
            )
            for day in days:
                future = pending.popleft()
                yield day, future.result
                # Repoe a janela so depois que o dia foi processado, para nao
                # buscar mais rapido do que se consegue gravar.
                nxt = next(upcoming, None)
                if nxt is not None:
                    pending.append(pool.submit(lambda d=nxt: list(fetch(client, d))))

    def _run(
        self,
        dataset: str,
        days: list[date],
        today: date,
        fetch: Callable[[PNCPClient, date], Iterator[dict[str, Any]]],
        normalize: Callable[[dict[str, Any]], dict[str, Any]],
        model: type[Ata] | type[Contrato],
        fields: DatasetFields,
        seen: set[str],
        ata_keys: tuple[set[str], set[str]] | None = None,
    ) -> FilterStats:
        stats = FilterStats()
        buffer: list[dict[str, Any]] = []

        with self.session_factory() as session:
            run = CollectionRun(
                dataset=dataset,
                window_start=days[0] if days else None,
                window_end=days[-1] if days else None,
            )
            session.add(run)
            session.commit()
            run_id = run.id

        with (
            self.session_factory() as session,
            make_progress(self.console) as progress,
        ):
            task = progress.add_task(
                f"Coletando {dataset}", total=len(days), detail="mantidos 0"
            )
            pages_task = progress.add_task(
                "Paginas", total=None, detail="0 dias conhecidos"
            )
            tracker = _PageTracker(progress, pages_task)

            with PNCPClient(self.config, on_page=tracker) as client:
                for day, records in self._iter_days(client, days, fetch):
                    try:
                        for raw in records():
                            self._handle_record(
                                raw=raw,
                                normalize=normalize,
                                model=model,
                                fields=fields,
                                today=today,
                                seen=seen,
                                ata_keys=ata_keys,
                                stats=stats,
                                buffer=buffer,
                                session=session,
                            )
                    except DayFetchError as exc:
                        # Um dia perdido nao derruba a varredura inteira; fica registrado.
                        stats.failed_days.append(day.isoformat())
                        logger.error("Dia %s ignorado: %s", day, exc)

                    progress.update(
                        task,
                        advance=1,
                        detail=f"mantidos {stats.kept} / descartados {stats.total_discarded}",
                    )

            if buffer:
                upsert(session, model, buffer)
                session.commit()

            stored = session.get(CollectionRun, run_id)
            if stored is not None:
                stored.finished_at = datetime.now(timezone.utc)
                stored.stats = stats.as_dict()
                session.commit()

        return stats


def render_summary(console: Console, dataset: str, stats: FilterStats) -> None:
    table = Table(title=f"Resumo - {dataset}")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    table.add_row("Registros recebidos", str(stats.fetched))
    table.add_row("Duplicados (dedup)", str(stats.duplicates))
    table.add_row("Persistidos", str(stats.kept))
    table.add_row("Suspeitos", str(stats.suspicious))
    if stats.derived_from_ata:
        table.add_row("Derivados de ata", str(stats.derived_from_ata))
    table.add_row("Descartados", str(stats.total_discarded))
    if stats.failed_days:
        table.add_row("Dias com falha", str(len(stats.failed_days)))
    for reason, count in stats.discarded.most_common():
        table.add_row(f"    {reason}", str(count))
    console.print(table)
