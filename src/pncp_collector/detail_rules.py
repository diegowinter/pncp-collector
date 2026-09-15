"""Regras de negocio da fase 2 (detalhamento). Funcoes puras: sem rede, sem banco.

Recebem linhas ja normalizadas (snake_case, ver schemas) e devolvem decisoes.
A orquestracao em detail.py so executa o que aqui se decide.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .config import Settings
from .models import DetailStatus, ItemsStatus, ResultsStatus

HOMOLOGADO = "Homologado"

ATA = "ata"
CONTRATO = "contrato"


# --- arquivo alvo (6.1) --------------------------------------------------------


def _split_types(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def target_file_types(config: Settings, documento_tipo: str) -> frozenset[str]:
    """Conjunto de tipoDocumentoNome que contam. Vazio = qualquer arquivo conta."""
    raw = config.target_file_types_ata if documento_tipo == ATA else config.target_file_types_contrato
    return _split_types(raw)


def is_target_file_type(tipo_documento_nome: Any, targets: frozenset[str]) -> bool:
    """Comparacao exata apos strip(); sem lower()/unaccent (nenhuma variacao vista).

    `statusAtivo` nao existe na resposta (docs/API_DETALHE.md, § 1.2), entao
    todo arquivo listado e candidato.
    """
    if not targets:
        return True
    if not isinstance(tipo_documento_nome, str):
        return False
    return tipo_documento_nome.strip() in targets


def has_target_file(arquivos: list[dict[str, Any]], targets: frozenset[str]) -> bool:
    """Ha pelo menos um arquivo do tipo alvo? Lista vazia -> False, mesmo com targets vazio."""
    return any(is_target_file_type(a.get("tipo_documento_nome"), targets) for a in arquivos)


# --- homologacao (6.2) ---------------------------------------------------------


def homologated_items(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in itens if i.get("situacao_compra_item_nome") == HOMOLOGADO]


def items_needing_results(itens: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Homologados com temResultado. Homologado sem resultado responde 204: nao gasta chamada."""
    return [i for i in homologated_items(itens) if i.get("tem_resultado")]


def decide_items_status(itens: list[dict[str, Any]]) -> str:
    return ItemsStatus.SEM_ITENS if not itens else ItemsStatus.OK


# --- vencedor (6.3) ------------------------------------------------------------


def _valid_price(value: Any) -> bool:
    if value is None:
        return False
    try:
        return Decimal(str(value)) > 0
    except (ArithmeticError, ValueError):
        return False


def pick_winner(resultados: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Entre os resultados com valor_unitario_homologado > 0, o de menor
    ordem_classificacao_srp (None conta como infinito); empate -> menor
    sequencial_resultado (fallback: posicao na lista)."""
    candidates = [
        (
            r["ordem_classificacao_srp"] if r.get("ordem_classificacao_srp") is not None else float("inf"),
            r["sequencial_resultado"] if r.get("sequencial_resultado") is not None else position,
            position,
            r,
        )
        for position, r in enumerate(resultados)
        if _valid_price(r.get("valor_unitario_homologado"))
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: c[:3])[3]


def mark_winner(resultados: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grava is_winner em cada resultado (True so no vencedor). Muta e devolve a lista."""
    winner = pick_winner(resultados)
    for r in resultados:
        r["is_winner"] = r is winner
    return resultados


# --- maquina de estados do documento (6.4) --------------------------------------


def decide_document_status(
    *,
    identified: bool,
    has_file: bool | None,
    error: str | None = None,
    items_status: str | None = None,
    total_homologados: int | None = None,
    results_status: str | None = None,
    items_error: str | None = None,
) -> tuple[str, str | None]:
    """(detail_status, detail_error) dado o que ja se sabe do documento e da compra.

    Ordem de precedencia: sem_identificacao, sem_arquivo, erro (do documento),
    sem_homologado, erro (da compra), ok. Se a compra ainda nao fechou (itens ou
    resultados pendentes/parciais), continua pendente.
    """
    if not identified:
        return DetailStatus.SEM_IDENTIFICACAO, None
    if has_file is False:
        return DetailStatus.SEM_ARQUIVO, None
    if error:
        return DetailStatus.ERRO, error
    if has_file is None or items_status in (None, ItemsStatus.PENDENTE):
        return DetailStatus.PENDENTE, None
    if items_status == ItemsStatus.SEM_ITENS or total_homologados == 0:
        return DetailStatus.SEM_HOMOLOGADO, None
    if items_status == ItemsStatus.ERRO:
        return DetailStatus.ERRO, items_error or "itens: erro"
    if results_status == ResultsStatus.OK:
        return DetailStatus.OK, None
    if results_status == ResultsStatus.ERRO:
        return DetailStatus.ERRO, "resultados: erro"
    return DetailStatus.PENDENTE, None  # resultados pendente/parcial


# --- politica de revisita (6.4) --------------------------------------------------

# Por padrao a fase 2 volta a olhar o que ainda nao fechou ou pode mudar sozinho.
# sem_arquivo e ok so com flag; sem_identificacao nunca (a capa nao muda sem fase 1).
DEFAULT_REVISIT = (DetailStatus.PENDENTE, DetailStatus.ERRO, DetailStatus.SEM_HOMOLOGADO)


def revisit_statuses(refresh: bool = False, revisit_sem_arquivo: bool = False) -> tuple[str, ...]:
    statuses = list(DEFAULT_REVISIT)
    if revisit_sem_arquivo:
        statuses.append(DetailStatus.SEM_ARQUIVO)
    if refresh:
        statuses.append(DetailStatus.OK)
    return tuple(statuses)


# --- contadores da execucao (7.6) ------------------------------------------------


@dataclass
class DetailStats:
    """Espelho de FilterStats para a fase 2."""

    documents_before: Counter = field(default_factory=Counter)
    documents_after: Counter = field(default_factory=Counter)
    documents_processed: int = 0
    compras_fetched: int = 0
    items_stored: int = 0
    results_stored: int = 0
    files_stored: int = 0
    calls: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    step_seconds: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents_before": dict(self.documents_before),
            "documents_after": dict(self.documents_after),
            "documents_processed": self.documents_processed,
            "compras_fetched": self.compras_fetched,
            "items_stored": self.items_stored,
            "results_stored": self.results_stored,
            "files_stored": self.files_stored,
            "calls": dict(self.calls),
            "errors": dict(self.errors),
            "step_seconds": dict(self.step_seconds),
        }
