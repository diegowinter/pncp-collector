"""Filtros locais aplicados a cada registro antes da persistencia."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .config import Settings


class DiscardReason:
    NO_SIGNATURE = "sem_data_assinatura"
    OLD_SIGNATURE = "assinatura_fora_da_janela"
    CANCELLED = "cancelado"
    NO_ID = "sem_identificador"
    DERIVED_FROM_ATA = "contrato_derivado_de_ata"


class SuspicionReason:
    LONG_VALIDITY = "vigencia_maior_que_limite"
    VALIDITY_BEFORE_SIGNATURE = "vigencia_inicia_antes_da_assinatura"


@dataclass
class FilterStats:
    """Contadores da execucao, exibidos ao final."""

    fetched: int = 0
    duplicates: int = 0
    kept: int = 0
    suspicious: int = 0
    derived_from_ata: int = 0
    failed_days: list[str] = field(default_factory=list)
    discarded: Counter = field(default_factory=Counter)

    @property
    def total_discarded(self) -> int:
        return sum(self.discarded.values())

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "duplicates": self.duplicates,
            "kept": self.kept,
            "suspicious": self.suspicious,
            "derived_from_ata": self.derived_from_ata,
            "failed_days": self.failed_days,
            "discarded": dict(self.discarded),
        }


def check_record(
    record: dict[str, Any], config: Settings, today: date | None = None
) -> tuple[str | None, list[str]]:
    """Retorna (motivo_do_descarte | None, motivos_de_suspeita)."""
    today = today or date.today()

    if not record.get("pncp_id"):
        return DiscardReason.NO_ID, []

    # Corte que define a amostra: assinatura nos ultimos N dias.
    # Sem data de assinatura nao ha como atestar a atualidade do preco.
    signed_at = record.get("signed_at")
    if signed_at is None:
        return DiscardReason.NO_SIGNATURE, []
    if signed_at < today - timedelta(days=config.signature_max_age_days):
        return DiscardReason.OLD_SIGNATURE, []

    if record.get("cancelled") or record.get("cancelled_at") is not None:
        return DiscardReason.CANCELLED, []

    reasons: list[str] = []
    start, end = record.get("validity_start"), record.get("validity_end")
    if start and end and (end - start).days > config.suspicious_validity_days:
        reasons.append(SuspicionReason.LONG_VALIDITY)
    if start and start < signed_at:
        reasons.append(SuspicionReason.VALIDITY_BEFORE_SIGNATURE)

    return None, reasons
