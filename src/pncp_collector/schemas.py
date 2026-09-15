"""Do payload da API para a linha do banco.

A unica transformacao de nome e camelCase -> snake_case; objetos aninhados sao
achatados carregando o caminho no nome (`orgaoEntidade.razaoSocial` ->
`orgao_entidade_razao_social`). Os valores vao como vieram, so convertidos para o
tipo da coluna. Nada de renomear campo do PNCP.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Boolean, Date, Integer, Numeric

from .models import Ata, Base, Contrato

_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def to_snake(name: str) -> str:
    """camelCase -> snake_case. `numeroControlePNCPAta` -> `numero_controle_pncp_ata`."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def parse_date(value: Any) -> date | None:
    """Aceita 'yyyy-MM-dd' e ISO com hora; o que nao for data vira None."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def parse_decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def parse_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def flatten(payload: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Achata o payload em chaves snake_case, mantendo o caminho no nome."""
    flat: dict[str, Any] = {}
    for key, value in payload.items():
        name = f"{prefix}{to_snake(key)}"
        if isinstance(value, dict):
            flat.update(flatten(value, prefix=f"{name}_"))
        else:
            flat[name] = value
    return flat


def _coerce(value: Any, column: Any) -> Any:
    kind = column.type
    if isinstance(kind, Date):
        return parse_date(value)
    if isinstance(kind, Numeric):
        return parse_decimal(value)
    if isinstance(kind, Integer):
        return parse_int(value)
    if isinstance(kind, Boolean):
        return None if value is None else bool(value)
    return value


def normalize(raw: dict[str, Any], model: type[Base]) -> dict[str, Any]:
    """Monta a linha a partir das colunas do modelo que existem no payload."""
    flat = flatten(raw)
    record: dict[str, Any] = {}

    for column in model.__table__.columns:
        if column.name in CONTROL_COLUMNS or column.name not in flat:
            continue
        value = _coerce(flat[column.name], column)
        # Coluna obrigatoria com valor nulo fica de fora: ou o default assume,
        # ou o registro ja vai ser descartado pelos filtros locais.
        if value is None and not column.nullable:
            continue
        record[column.name] = value

    record["raw"] = raw
    return record


# Colunas nossas: nunca vem do payload.
CONTROL_COLUMNS = frozenset(
    {
        "collected_at",
        "updated_at",
        "raw",
        "suspicious",
        "suspicious_reasons",
        "derived_from_ata",
        "derivation_match",
    }
)


def normalize_ata(raw: dict[str, Any]) -> dict[str, Any]:
    return normalize(raw, Ata)


def normalize_contrato(raw: dict[str, Any]) -> dict[str, Any]:
    return normalize(raw, Contrato)
