"""Fase 3: triagem das compras pelo objeto da capa.

Dois passos, ambos retomaveis:

    S  semear: cria a compra a partir das capas (chave + objeto), sem API
    T  triar: uma chamada ao modelo por compra pendente, grava status + probabilidades

Le e grava so no banco; a unica rede e o endpoint de decisoes (decisions.py).
As regras de decisao estao em triage_rules; aqui so se executa e se persiste.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rich.console import Console
from rich.table import Table

from .config import Settings, settings as default_settings
from .db import (
    DetailRepository,
    TriageRow,
    build_engine,
    build_session_factory,
    create_schema,
)
from .decisions import DecisionsClient, DecisionsError, DecisionsResult
from .memory import release_heap
from .models import TriageStatus
from .pipeline import make_progress
from .triage_rules import (
    TriageDecision,
    TriageStats,
    as_decimal,
    build_questions,
    build_state,
    decide,
    suggested_groups,
)

logger = logging.getLogger(__name__)

DATASET = "triagem"
BATCH_SIZE = 100
DEFAULT_STATUSES = (TriageStatus.PENDENTE, TriageStatus.ERRO)


class CostCapReached(RuntimeError):
    pass


@dataclass
class TriageOutcome:
    row: TriageRow
    decision: TriageDecision
    result: DecisionsResult | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Triager:
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
        self.client_factory = client_factory or self._default_client
        self._session_factory = None
        self.questions = build_questions()

    @contextmanager
    def _default_repository(self) -> Iterator[DetailRepository]:
        if self._session_factory is None:
            engine = build_engine(self.config)
            create_schema(engine)
            self._session_factory = build_session_factory(engine)
        with self._session_factory() as session:
            yield DetailRepository(session)

    @staticmethod
    def _default_client(config: Settings) -> DecisionsClient:
        return DecisionsClient(
            config.openrouter_api_key or "",
            base_url=config.openrouter_base_url,
            model=config.triage_model,
            timeout=config.timeout,
        )

    # --- execucao ------------------------------------------------------------

    def run(
        self,
        statuses: Sequence[str] | None = None,
        limit: int | None = None,
        seed: bool = True,
        triage: bool = True,
    ) -> TriageStats:
        statuses = tuple(statuses or DEFAULT_STATUSES)
        stats = TriageStats()
        started = time.perf_counter()

        with self.repository_factory() as repo:
            run_id = repo.start_run(DATASET)
            stats.compras_before.update(repo.count_compras_by_triage())
            if seed:
                with self.console.status("Semeando compras a partir das capas..."):
                    stats.seeded = repo.seed_compras_from_capas()
                self.console.print(
                    f"[dim]{stats.seeded} compras criadas ou com objeto preenchido.[/dim]"
                )
            if triage:
                rows = repo.select_compras_for_triage(
                    statuses, max_attempts=self.config.triage_max_attempts, limit=limit
                )
                if rows:
                    with self.client_factory(self.config) as client:
                        self._triage(client, repo, rows, stats)
            stats.compras_after.update(repo.count_compras_by_triage())
            stats.seconds = time.perf_counter() - started
            repo.finish_run(run_id, stats.as_dict())
        return stats

    def _triage(
        self, client: Any, repo: DetailRepository, rows: Sequence[TriageRow], stats: TriageStats
    ) -> None:
        workers = max(1, min(self.config.triage_concurrency, len(rows)))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            with make_progress(self.console) as progress:
                task = progress.add_task("Triando compras", total=len(rows), detail="")
                for chunk in _chunks(rows, BATCH_SIZE):
                    outcomes = list(pool.map(lambda r: self._ask(client, r), chunk))
                    self._persist(repo, outcomes, stats)
                    progress.update(
                        task,
                        advance=len(chunk),
                        detail=(
                            f"potencial {stats.decided[TriageStatus.POTENCIAL]} / "
                            f"indefinido {stats.decided[TriageStatus.INDEFINIDO]} / "
                            f"erros {sum(stats.errors.values())} / US$ {stats.cost_usd:.3f}"
                        ),
                    )
                    cap = self.config.triage_max_cost_usd
                    if cap is not None and stats.cost_usd >= cap:
                        self.console.print(
                            f"[yellow]Teto de gasto atingido (US$ {stats.cost_usd:.3f} >= "
                            f"{cap}); parando. O restante fica pendente.[/yellow]"
                        )
                        break
        except BaseException:
            client.stop()
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()

    def _ask(self, client: Any, row: TriageRow) -> TriageOutcome:
        try:
            result = client.decide(build_state(row.objeto), self.questions)
        except DecisionsError as exc:
            return TriageOutcome(
                row=row,
                decision=TriageDecision(status=TriageStatus.ERRO, error=str(exc)[:500]),
            )
        decision = decide(
            result.answers,
            potential_threshold=self.config.triage_potential_threshold,
            group_threshold=self.config.triage_group_threshold,
        )
        return TriageOutcome(row=row, decision=decision, result=result)

    def _persist(
        self, repo: DetailRepository, outcomes: Sequence[TriageOutcome], stats: TriageStats
    ) -> None:
        now = _now()
        updates: list[dict[str, Any]] = []
        for out in outcomes:
            d = out.decision
            stats.processed += 1
            stats.decided[d.status] += 1
            row: dict[str, Any] = {
                "compra_key": out.row.compra_key,
                "triage_status": d.status,
                "triage_error": d.error,
                "triage_attempts": out.row.triage_attempts + 1,
                "triaged_at": now,
                "triage_model": self.config.triage_model,
            }
            if out.result is not None:
                stats.calls += 1
                stats.cost_usd += out.result.cost_usd
                stats.input_tokens += out.result.input_tokens
                stats.output_tokens += out.result.output_tokens
                row["triage_model"] = out.result.model or self.config.triage_model
                row["triage_raw"] = {
                    "answers": out.result.answers,
                    "usage": out.result.raw.get("usage"),
                    "id": out.result.raw.get("id"),
                }
            if d.status == TriageStatus.ERRO:
                stats.errors[_error_kind(d.error)] += 1
            else:
                row.update(
                    triage_clarity=d.clarity,
                    triage_potential=as_decimal(d.potential),
                    triage_groups=d.groups,
                )
            updates.append(row)
        repo.update_triage(updates)
        repo.commit()
        release_heap()


def _error_kind(error: str | None) -> str:
    if not error:
        return "desconhecido"
    if error.startswith("HTTP "):
        return error.split(":", 1)[0]
    return error.split(":", 1)[0][:40]


def render_triage_summary(console: Console, stats: TriageStats, config: Settings) -> None:
    table = Table(title="Resumo - triagem")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    table.add_row("Compras semeadas/objeto preenchido", str(stats.seeded))
    table.add_row("Compras triadas", str(stats.processed))
    for status, count in sorted(stats.decided.items()):
        table.add_row(f"    {status}", str(count))
    table.add_row("Chamadas ao modelo", str(stats.calls))
    for kind, count in sorted(stats.errors.items()):
        table.add_row(f"    erros {kind}", str(count))
    table.add_row("Tokens (entrada / saida)", f"{stats.input_tokens} / {stats.output_tokens}")
    table.add_row("Custo (US$)", f"{stats.cost_usd:.4f}")
    if stats.processed:
        table.add_row("Custo por compra (US$)", f"{stats.cost_usd / stats.processed:.6f}")
    table.add_row("Tempo", f"{stats.seconds:.0f}s")
    console.print(table)

    status_table = Table(title="Compras por triage_status (antes -> depois)")
    status_table.add_column("Status")
    status_table.add_column("Antes", justify="right")
    status_table.add_column("Depois", justify="right")
    for key in sorted(set(stats.compras_before) | set(stats.compras_after)):
        status_table.add_row(
            key, str(stats.compras_before.get(key, 0)), str(stats.compras_after.get(key, 0))
        )
    console.print(status_table)


__all__ = ["Triager", "TriageOutcome", "render_triage_summary", "suggested_groups"]
