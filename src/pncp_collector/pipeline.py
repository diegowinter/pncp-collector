"""Orquestracao da varredura dia a dia, com progresso em rich."""

from __future__ import annotations

import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, TypeVar

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
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from .client import CollectionInterrupted, DayFetchError, PNCPClient
from .config import Settings, settings as default_settings
from .db import (
    build_engine,
    build_session_factory,
    create_schema,
    latest_reference,
    load_ata_keys,
    load_done_days,
    load_partial_days,
    mark_day_done,
    save_day_progress,
    upsert,
)
from .memory import release_heap
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

# Tentativas de uma transacao quando a conexao com o banco cai (backoff
# exponencial ate DB_MAX_BACKOFF segundos entre elas). Os upserts sao
# idempotentes, entao repetir a transacao inteira e seguro.
DB_RETRIES = 8
DB_MAX_BACKOFF = 60.0

# A cada N paginas gravadas, devolve ao sistema o heap que a glibc retem
# (ver memory.py). Medido: sem isso o RSS sobe ~0,4 MB por pagina.
TRIM_EVERY_PAGES = 20

T = TypeVar("T")

# (cliente, dia, pagina inicial) -> (pagina, totalPaginas, registros) por pagina
PageFetch = Callable[[PNCPClient, date, int], Iterator[tuple[int, int, list[dict[str, Any]]]]]


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

    def days_for(self, dataset: str, reference: date) -> list[date]:
        return self.ata_days(reference) if dataset == "atas" else self.contrato_days(reference)

    def pending_reference(self, datasets: list[str]) -> date | None:
        """Referencia da ultima varredura que ficou incompleta, se houver.

        Olha so a referencia mais recente: comecar uma varredura nova (com
        --reference) abandona a anterior.
        """
        with self.session_factory() as session:
            reference = latest_reference(session, datasets)
            if reference is None:
                return None
            for dataset in datasets:
                done = load_done_days(session, dataset, reference)
                if not set(self.days_for(dataset, reference)) <= done:
                    return reference
        return None

    # --- banco: transacoes curtas -----------------------------------------------

    def _transaction(self, what: str, work: Callable[[Session], T]) -> T:
        """Abre uma sessao, executa `work`, commita e fecha.

        Cada escrita da varredura e uma transacao propria, em vez de uma sessao
        aberta pela varredura inteira: entre uma pagina e outra o coletor pode
        passar minutos esperando a API, e uma conexao parada esse tempo todo e
        derrubada por NAT/firewall sem aviso. Com a sessao curta, cada checkout
        passa pelo pre-ping do pool e troca a conexao morta por uma nova.

        Se mesmo assim a conexao cair no meio (OperationalError/InterfaceError),
        a transacao e repetida com backoff. So depois de DB_RETRIES o erro sobe.
        """
        for attempt in itertools.count():
            try:
                with self.session_factory() as session:
                    result = work(session)
                    session.commit()
                    return result
            except (OperationalError, InterfaceError) as exc:
                if attempt + 1 >= DB_RETRIES:
                    raise
                wait = min(2**attempt + 1.0, DB_MAX_BACKOFF)
                logger.warning(
                    "Banco indisponivel ao gravar %s (%s); tentativa %d de %d em %.0fs",
                    what,
                    str(exc.orig or exc).strip().splitlines()[0],
                    attempt + 2,
                    DB_RETRIES,
                    wait,
                )
                time.sleep(wait)
        raise AssertionError("unreachable")

    # --- execucao ------------------------------------------------------------

    def collect_atas(self, today: date | None = None) -> FilterStats:
        today = today or date.today()

        def fetch(client: PNCPClient, day: date, start_page: int):
            return client.iter_atas_pages(day, start_page)

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

        # Sao centenas de milhares de chaves: na caixa ARM leva uns 2 minutos
        # sem nenhuma barra na tela, o que parece travamento.
        with self.console.status("Carregando chaves das atas para o dedup de contratos..."):
            ata_keys = self._transaction("chaves das atas", load_ata_keys)
        self.console.print(
            f"[dim]{len(ata_keys[0])} atas e {len(ata_keys[1])} compras com ata carregadas.[/dim]"
        )

        def fetch(client: PNCPClient, day: date, start_page: int):
            return client.iter_contratos_pages(day, start_page)

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
        fields: DatasetFields,
        today: date,
        seen: set[str],
        ata_keys: tuple[set[str], set[str]] | None,
        stats: FilterStats,
        buffer: list[dict[str, Any]],
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

    # --- workers: baixam, nunca gravam -----------------------------------------

    @staticmethod
    def _put(out: queue.Queue[_DayEvent], stop: threading.Event, event: _DayEvent) -> None:
        """put na fila limitada, mas desistindo se a coleta for interrompida."""
        while True:
            try:
                out.put(event, timeout=0.2)
                return
            except queue.Full:
                if stop.is_set():
                    raise CollectionInterrupted() from None

    def _fetch_day(
        self,
        client: PNCPClient,
        fetch: PageFetch,
        day: date,
        start_page: int,
        out: queue.Queue[_DayEvent],
        stop: threading.Event,
    ) -> None:
        """Um dia, pagina a pagina. Cada pagina vai para a fila assim que chega."""
        try:
            for page, total_pages, records in fetch(client, day, start_page):
                self._put(out, stop, _DayEvent(day, page, total_pages, records))
            self._put(out, stop, _DayEvent(day, done=True))
        except DayFetchError as exc:
            self._put(out, stop, _DayEvent(day, error=exc))
        except CollectionInterrupted:
            pass  # quem interrompeu foi a thread principal; ela ja esta saindo
        except BaseException as exc:
            # Inesperado: sobe para a thread principal em vez de morrer calado.
            self._put(out, stop, _DayEvent(day, error=exc, fatal=True))

    # --- execucao --------------------------------------------------------------

    def _run(
        self,
        dataset: str,
        days: list[date],
        today: date,
        fetch: PageFetch,
        normalize: Callable[[dict[str, Any]], dict[str, Any]],
        model: type[Ata] | type[Contrato],
        fields: DatasetFields,
        seen: set[str],
        ata_keys: tuple[set[str], set[str]] | None = None,
    ) -> FilterStats:
        """Varre `days` com PNCP_LIST_CONCURRENCY dias em paralelo, um dia por thread.

        As threads so baixam: cada pagina entra numa fila limitada e so a
        thread principal toca o banco, filtrando e gravando pagina a pagina. A
        fila limitada segura a memoria (quem baixa espera se a gravacao
        atrasar), e cada commit leva junto a ultima pagina gravada do dia, para
        uma retomada continuar dali. Cada commit e uma transacao curta e
        propria (ver _transaction): nao ha conexao aberta esperando a API.
        """
        stats = FilterStats()
        buffer: list[dict[str, Any]] = []

        def load_checkpoint(session: Session) -> tuple[set[date], dict[date, int]]:
            return (
                load_done_days(session, dataset, today),
                load_partial_days(session, dataset, today),
            )

        done, partial = self._transaction("checkpoint", load_checkpoint)
        todo = [day for day in days if day not in done]
        resumed_pages = sum(partial.get(day, 0) for day in todo)
        if len(todo) < len(days) or resumed_pages:
            self.console.print(
                f"[yellow]Retomando {dataset} (referencia {today}): "
                f"{len(days) - len(todo)} de {len(days)} dias ja concluidos"
                + (f", {resumed_pages} paginas de dias em andamento." if resumed_pages else ".")
                + "[/yellow]"
            )

        def start_run(session: Session) -> int:
            run = CollectionRun(
                dataset=dataset,
                window_start=days[0] if days else None,
                window_end=days[-1] if days else None,
            )
            session.add(run)
            session.flush()
            return run.id

        run_id = self._transaction("inicio da execucao", start_run)

        with make_progress(self.console) as progress:
            task = progress.add_task(
                f"Coletando {dataset}",
                total=len(days),
                completed=len(days) - len(todo),
                detail="mantidos 0",
            )
            pages_task = progress.add_task(
                "Paginas", total=None, detail="0 dias conhecidos"
            )
            tracker = _PageTracker(progress, pages_task)

            def show() -> None:
                progress.update(
                    task,
                    detail=f"mantidos {stats.kept} / descartados {stats.total_discarded}",
                )

            with PNCPClient(self.config, on_page=tracker) as client:
                workers = max(1, min(self.config.list_concurrency, len(todo)))
                events: queue.Queue[_DayEvent] = queue.Queue(maxsize=workers * 2)
                stop = threading.Event()
                upcoming = iter(todo)
                kept_by_day: dict[date, int] = {}
                pages_saved = 0
                active = 0
                pool = ThreadPoolExecutor(max_workers=workers)

                def start_next() -> None:
                    nonlocal active
                    day = next(upcoming, None)
                    if day is None:
                        return
                    kept_by_day[day] = 0
                    first = partial.get(day, 0) + 1
                    pool.submit(self._fetch_day, client, fetch, day, first, events, stop)
                    active += 1

                try:
                    for _ in range(workers):
                        start_next()

                    while active:
                        try:
                            # Com timeout: no Windows um get() sem prazo nao
                            # deixa o Ctrl+C chegar.
                            event = events.get(timeout=0.5)
                        except queue.Empty:
                            continue

                        if event.fatal:
                            raise event.error  # type: ignore[misc]

                        if event.error is not None or event.done:
                            if event.error is not None:
                                # Um dia perdido nao derruba a varredura. As paginas
                                # ja gravadas ficam; a retomada continua dali.
                                stats.failed_days.append(event.day.isoformat())
                                logger.error("Dia %s ignorado: %s", event.day, event.error)
                            else:
                                day, kept = event.day, kept_by_day[event.day]
                                self._transaction(
                                    f"fim do dia {day}",
                                    lambda s: mark_day_done(s, dataset, today, day, kept),
                                )
                            active -= 1
                            progress.update(task, advance=1)
                            show()
                            start_next()
                            continue

                        kept_before = stats.kept
                        for raw in event.records:
                            self._handle_record(
                                raw=raw,
                                normalize=normalize,
                                fields=fields,
                                today=today,
                                seen=seen,
                                ata_keys=ata_keys,
                                stats=stats,
                                buffer=buffer,
                            )
                        kept_by_day[event.day] += stats.kept - kept_before
                        # Checkpoint: os registros da pagina e o "ate aqui" do dia
                        # vao no mesmo commit.
                        def save_page(session: Session, page: _DayEvent = event) -> None:
                            if buffer:
                                upsert(session, model, buffer)
                            save_day_progress(
                                session, dataset, today, page.day, page.page, page.total_pages
                            )

                        self._transaction(f"pagina {event.page} de {event.day}", save_page)
                        buffer.clear()
                        pages_saved += 1
                        if pages_saved % TRIM_EVERY_PAGES == 0:
                            release_heap()
                        show()
                except BaseException:
                    # Ctrl+C: as threads param de esperar (retry, fila cheia) e o
                    # que nem comecou e descartado, em vez de esperar os dias em voo.
                    stop.set()
                    client.stop()
                    pool.shutdown(wait=False, cancel_futures=True)
                    raise
                pool.shutdown()

        def finish_run(session: Session) -> None:
            stored = session.get(CollectionRun, run_id)
            if stored is not None:
                stored.finished_at = datetime.now(timezone.utc)
                stored.stats = stats.as_dict()

        self._transaction("fim da execucao", finish_run)
        return stats


@dataclass
class _DayEvent:
    """O que um worker manda para a thread principal: uma pagina, o fim do dia ou um erro."""

    day: date
    page: int = 0
    total_pages: int = 0
    records: list[dict[str, Any]] | None = None
    done: bool = False
    error: BaseException | None = None
    fatal: bool = False

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
