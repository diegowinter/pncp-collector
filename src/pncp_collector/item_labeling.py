"""Fase 4: rotulagem de descricoes de item pelo modelo de decisoes.

Dois passos, ambos retomaveis:

    S  semear: cria em `item_labels` as descricoes distintas da amostra de treino
       (fatias peneira, reserva e aleatorio), sem API
    R  rotular: uma chamada ao modelo por descricao pendente, grava categoria e
       probabilidades

Le e grava so no banco; a unica rede e o endpoint de decisoes (decisions.py).
As regras estao em item_rules; aqui so se executa e se persiste. O desenho
espelha triage.py de proposito: mesma camada de cliente, mesmo teto de gasto.
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
from .db import DetailRepository, ItemLabelRow, build_engine, build_session_factory, create_schema
from .decisions import DecisionsClient, DecisionsError, DecisionsResult
from .item_rules import (
    SIEVE_PATTERN,
    ItemLabelDecision,
    ItemLabelStats,
    build_item_questions,
    build_item_state,
    decide_item,
)
from .memory import release_heap
from .models import LabelStatus
from .pipeline import make_progress
from .triage_rules import as_decimal

logger = logging.getLogger(__name__)

DATASET = "rotulos_itens"
BATCH_SIZE = 100
DEFAULT_STATUSES = (LabelStatus.PENDENTE, LabelStatus.ERRO)
# Fatias semeadas pela regex (SEED_SLICES) mais `ativo`: descricoes escolhidas pelo que o
# classificador local marca com duvida ou exagera (aprendizado ativo), inseridas por script.
SEED_SLICES = ("peneira", "reserva", "aleatorio")
SLICES = SEED_SLICES + ("ativo",)


class CostCapReached(RuntimeError):
    pass


@dataclass
class LabelOutcome:
    row: ItemLabelRow
    decision: ItemLabelDecision
    result: DecisionsResult | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _chunks(items: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class ItemLabeler:
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
        self.questions = build_item_questions()

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
        label: bool = True,
        sample_slice: str | None = None,
        sample_reserva: int = 10_000,
        sample_aleatorio: int = 40_000,
    ) -> ItemLabelStats:
        """`sample_reserva` e `sample_aleatorio` sao os tamanhos ALVO das fatias; a
        semeadura so acrescenta o que falta para chegar neles (ou nada, se ja
        passou). A peneira entra inteira. `sample_slice` rotula so uma fatia."""
        statuses = tuple(statuses or DEFAULT_STATUSES)
        stats = ItemLabelStats()
        started = time.perf_counter()

        with self.repository_factory() as repo:
            run_id = repo.start_run(DATASET)
            stats.labels_before.update(repo.count_item_labels())
            if seed:
                self._seed(repo, stats, sample_reserva, sample_aleatorio)
            if label:
                rows = repo.select_item_labels(
                    statuses,
                    max_attempts=self.config.triage_max_attempts,
                    limit=limit,
                    sample_slice=sample_slice,
                )
                if rows:
                    with self.client_factory(self.config) as client:
                        self._label(client, repo, rows, stats)
            stats.labels_after.update(repo.count_item_labels())
            stats.seconds = time.perf_counter() - started
            repo.finish_run(run_id, stats.as_dict())
        return stats

    def _seed(
        self, repo: DetailRepository, stats: ItemLabelStats, reserva: int, aleatorio: int
    ) -> None:
        existing = _slice_totals(repo.count_item_labels())
        targets = {"peneira": None, "reserva": reserva, "aleatorio": aleatorio}
        for sample_slice in SEED_SLICES:
            target = targets[sample_slice]
            limit = None if target is None else max(0, target - existing.get(sample_slice, 0))
            if limit == 0:
                continue
            with self.console.status(f"Semeando fatia {sample_slice}..."):
                stats.seeded[sample_slice] = repo.seed_item_labels(
                    sample_slice, SIEVE_PATTERN, limit
                )
            self.console.print(
                f"[dim]{stats.seeded[sample_slice]} descricoes novas na fatia {sample_slice}.[/dim]"
            )

    def _label(
        self, client: Any, repo: DetailRepository, rows: Sequence[ItemLabelRow], stats: ItemLabelStats
    ) -> None:
        workers = max(1, min(self.config.triage_concurrency, len(rows)))
        pool = ThreadPoolExecutor(max_workers=workers)
        try:
            with make_progress(self.console) as progress:
                task = progress.add_task("Rotulando descricoes", total=len(rows), detail="")
                for chunk in _chunks(rows, BATCH_SIZE):
                    outcomes = list(pool.map(lambda r: self._ask(client, r), chunk))
                    self._persist(repo, outcomes, stats)
                    interesse = stats.processed - stats.categories.get("outros", 0) - sum(
                        stats.errors.values()
                    )
                    progress.update(
                        task,
                        advance=len(chunk),
                        detail=(
                            f"de interesse {interesse} / erros {sum(stats.errors.values())} / "
                            f"US$ {stats.cost_usd:.3f}"
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

    def _ask(self, client: Any, row: ItemLabelRow) -> LabelOutcome:
        state = build_item_state(
            row.descricao,
            objeto=row.objeto,
            unidade_medida=row.unidade_medida,
            valor_unitario_estimado=row.valor_unitario_estimado,
            material_ou_servico_nome=row.material_ou_servico_nome,
        )
        try:
            result = client.decide(state, self.questions)
        except DecisionsError as exc:
            return LabelOutcome(
                row=row, decision=ItemLabelDecision(status=LabelStatus.ERRO, error=str(exc)[:500])
            )
        return LabelOutcome(row=row, decision=decide_item(result.answers), result=result)

    def _persist(
        self, repo: DetailRepository, outcomes: Sequence[LabelOutcome], stats: ItemLabelStats
    ) -> None:
        now = _now()
        updates: list[dict[str, Any]] = []
        for out in outcomes:
            d = out.decision
            stats.processed += 1
            stats.decided[d.status] += 1
            row: dict[str, Any] = {
                "descricao_norm": out.row.descricao_norm,
                "label_status": d.status,
                "label_error": d.error,
                "label_attempts": out.row.label_attempts + 1,
                "labeled_at": now,
                "label_model": self.config.triage_model,
            }
            if out.result is not None:
                stats.calls += 1
                stats.cost_usd += out.result.cost_usd
                stats.input_tokens += out.result.input_tokens
                stats.output_tokens += out.result.output_tokens
                row["label_model"] = out.result.model or self.config.triage_model
                row["label_raw"] = {
                    "answers": out.result.answers,
                    "usage": out.result.raw.get("usage"),
                    "id": out.result.raw.get("id"),
                }
            if d.status == LabelStatus.ERRO:
                stats.errors[_error_kind(d.error)] += 1
            else:
                stats.categories[d.category or "?"] += 1
                row.update(
                    label_category=d.category,
                    label_confidence=as_decimal(d.confidence),
                    label_interest=as_decimal(d.interest),
                    label_probabilities=d.probabilities,
                )
            updates.append(row)
        repo.update_item_labels(updates)
        repo.commit()
        release_heap()


def _slice_totals(counts: dict[str, int]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for key, n in counts.items():
        sample_slice = key.split(":", 1)[0]
        totals[sample_slice] = totals.get(sample_slice, 0) + n
    return totals


def _error_kind(error: str | None) -> str:
    if not error:
        return "desconhecido"
    if error.startswith("HTTP "):
        return error.split(":", 1)[0]
    return error.split(":", 1)[0][:40]


def render_label_summary(console: Console, stats: ItemLabelStats) -> None:
    table = Table(title="Resumo - rotulagem de itens")
    table.add_column("Metrica")
    table.add_column("Valor", justify="right")
    for sample_slice, n in sorted(stats.seeded.items()):
        table.add_row(f"Semeadas ({sample_slice})", str(n))
    table.add_row("Descricoes rotuladas", str(stats.processed))
    for category, count in stats.categories.most_common():
        table.add_row(f"    {category}", str(count))
    table.add_row("Chamadas ao modelo", str(stats.calls))
    for kind, count in sorted(stats.errors.items()):
        table.add_row(f"    erros {kind}", str(count))
    table.add_row("Tokens (entrada / saida)", f"{stats.input_tokens} / {stats.output_tokens}")
    table.add_row("Custo (US$)", f"{stats.cost_usd:.4f}")
    if stats.calls:
        table.add_row("Custo por descricao (US$)", f"{stats.cost_usd / stats.calls:.6f}")
    table.add_row("Tempo", f"{stats.seconds:.0f}s")
    console.print(table)

    status_table = Table(title="item_labels por fatia:status (antes -> depois)")
    status_table.add_column("Fatia:status")
    status_table.add_column("Antes", justify="right")
    status_table.add_column("Depois", justify="right")
    for key in sorted(set(stats.labels_before) | set(stats.labels_after)):
        status_table.add_row(
            key, str(stats.labels_before.get(key, 0)), str(stats.labels_after.get(key, 0))
        )
    console.print(status_table)


__all__ = ["ItemLabeler", "LabelOutcome", "SEED_SLICES", "SLICES", "render_label_summary"]
