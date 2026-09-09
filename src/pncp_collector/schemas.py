"""Normalizacao dos payloads da API para o modelo interno."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def parse_date(value: Any) -> date | None:
    """Aceita 'yyyy-MM-dd' e ISO com hora; datas absurdas viram None."""
    if value in (None, ""):
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y%m%d"):
        try:
            return datetime.strptime(text[: len(fmt) + 6], fmt).date()
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


def _digits(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return "".join(ch for ch in str(value) if ch.isalnum()) or None


def normalize_ata(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "pncp_id": raw.get("numeroControlePNCPAta"),
        "compra_pncp_id": raw.get("numeroControlePNCPCompra"),
        "numero_ata": raw.get("numeroAtaRegistroPreco"),
        "ano": raw.get("anoAta"),
        "signed_at": parse_date(raw.get("dataAssinatura")),
        "validity_start": parse_date(raw.get("vigenciaInicio")),
        "validity_end": parse_date(raw.get("vigenciaFim")),
        "published_at": parse_date(raw.get("dataPublicacaoPncp")),
        "updated_at_source": parse_date(raw.get("dataAtualizacao")),
        "cancelled": bool(raw.get("cancelado")),
        "cancelled_at": parse_date(raw.get("dataCancelamento")),
        "object_description": raw.get("objetoContratacao"),
        "agency_cnpj": _digits(raw.get("cnpjOrgao")),
        "agency_name": raw.get("nomeOrgao"),
        "unit_code": raw.get("codigoUnidadeOrgao"),
        "unit_name": raw.get("nomeUnidadeOrgao"),
        "source_user": raw.get("usuario"),
        "adhesion_allowed": raw.get("possibilidadeAdesao"),
        "raw": raw,
    }


def normalize_contrato(raw: dict[str, Any]) -> dict[str, Any]:
    agency = raw.get("orgaoEntidade") or {}
    unit = raw.get("unidadeOrgao") or {}
    contract_type = raw.get("tipoContrato") or {}

    return {
        "pncp_id": raw.get("numeroControlePNCP") or raw.get("numeroControlePncp"),
        "compra_pncp_id": raw.get("numeroControlePncpCompra")
        or raw.get("numeroControlePNCPCompra"),
        # Link direto com a ata de origem, quando o contrato deriva de uma.
        "ata_pncp_id": raw.get("numeroControlePncpAta") or raw.get("numeroControlePNCPAta"),
        "numero_contrato": raw.get("numeroContratoEmpenho"),
        "ano": raw.get("anoContrato"),
        "processo": raw.get("processo"),
        "signed_at": parse_date(raw.get("dataAssinatura")),
        # Contratos usam dataVigenciaInicio/Fim; atas usam vigenciaInicio/Fim.
        "validity_start": parse_date(
            raw.get("dataVigenciaInicio") or raw.get("vigenciaInicio")
        ),
        "validity_end": parse_date(raw.get("dataVigenciaFim") or raw.get("vigenciaFim")),
        "published_at": parse_date(raw.get("dataPublicacaoPncp")),
        "updated_at_source": parse_date(raw.get("dataAtualizacao")),
        "cancelled": bool(raw.get("cancelado")),
        "cancelled_at": parse_date(raw.get("dataCancelamento")),
        "object_description": raw.get("objetoContrato"),
        "supplier_document": _digits(raw.get("niFornecedor")),
        "supplier_name": raw.get("nomeRazaoSocialFornecedor"),
        "initial_value": parse_decimal(raw.get("valorInicial")),
        "global_value": parse_decimal(raw.get("valorGlobal")),
        "agency_cnpj": _digits(agency.get("cnpj") or raw.get("cnpjOrgao")),
        "agency_name": agency.get("razaoSocial") or raw.get("nomeOrgao"),
        "unit_code": unit.get("codigoUnidade"),
        "unit_name": unit.get("nomeUnidade"),
        "unit_uf": unit.get("ufSigla"),
        "unit_city": unit.get("municipioNome"),
        "contract_type": contract_type.get("nome"),
        "raw": raw,
    }
