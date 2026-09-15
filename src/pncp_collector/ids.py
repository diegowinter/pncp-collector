"""Numeros de controle do PNCP: parse e montagem das URLs de detalhe.

Formatos confirmados em docs/API_DETALHE.md, § 1.1:

    compra    {cnpj:14}-1-{seq:6}/{ano:4}            07954480000179-1-025970/2025
    ata       {compra}-{seqAta:6}                     07954480000179-1-025970/2025-000001
    contrato  {cnpj:14}-2-{seqContrato:6}/{ano:4}     07954480000179-2-031999/2026

O sequencial do contrato e do contrato, nunca da compra. Puro, sem rede.
"""

from __future__ import annotations

import re
from typing import NamedTuple

DETAIL_API = "/api/pncp/v1"
PUBLIC_SITE = "https://pncp.gov.br/app/editais"

_COMPRA = re.compile(r"^(?P<cnpj>\d{14})-1-(?P<seq>\d{6})/(?P<ano>\d{4})$")
_ATA = re.compile(r"^(?P<compra>\d{14}-1-\d{6}/\d{4})-(?P<seq_ata>\d{6})$")
_CONTRATO = re.compile(r"^(?P<cnpj>\d{14})-2-(?P<seq>\d{6})/(?P<ano>\d{4})$")


class CompraRef(NamedTuple):
    cnpj: str
    ano: int
    sequencial: int
    key: str


class AtaRef(NamedTuple):
    compra: CompraRef
    sequencial_ata: int
    key: str


class ContratoRef(NamedTuple):
    cnpj: str
    ano: int
    sequencial_contrato: int
    key: str


def _clean(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def parse_compra_key(value: object) -> CompraRef | None:
    """`numeroControlePNCPCompra` -> CompraRef; None para qualquer formato invalido."""
    text = _clean(value)
    match = _COMPRA.match(text) if text else None
    if match is None:
        return None
    return CompraRef(
        cnpj=match["cnpj"], ano=int(match["ano"]), sequencial=int(match["seq"]), key=text
    )


def parse_ata_key(value: object) -> AtaRef | None:
    """`numeroControlePNCPAta` -> AtaRef; a compra e o prefixo antes do ultimo `-`."""
    text = _clean(value)
    match = _ATA.match(text) if text else None
    if match is None:
        return None
    compra = parse_compra_key(match["compra"])
    if compra is None:
        return None
    return AtaRef(compra=compra, sequencial_ata=int(match["seq_ata"]), key=text)


def parse_contrato_key(value: object) -> ContratoRef | None:
    """`numeroControlePNCP` de contrato -> ContratoRef."""
    text = _clean(value)
    match = _CONTRATO.match(text) if text else None
    if match is None:
        return None
    return ContratoRef(
        cnpj=match["cnpj"], ano=int(match["ano"]), sequencial_contrato=int(match["seq"]), key=text
    )


# --- URLs (caminhos relativos a base_url; ver docs/API_DETALHE.md) -------------


def _compra_path(ref: CompraRef) -> str:
    return f"{DETAIL_API}/orgaos/{ref.cnpj}/compras/{ref.ano}/{ref.sequencial}"


def _contrato_path(ref: ContratoRef) -> str:
    return f"{DETAIL_API}/orgaos/{ref.cnpj}/contratos/{ref.ano}/{ref.sequencial_contrato}"


def url_arquivos_ata(ref: AtaRef) -> str:
    return f"{_compra_path(ref.compra)}/atas/{ref.sequencial_ata}/arquivos"


def url_arquivos_contrato(ref: ContratoRef) -> str:
    return f"{_contrato_path(ref)}/arquivos"


def url_itens(ref: CompraRef) -> str:
    return f"{_compra_path(ref)}/itens"


def url_resultados(ref: CompraRef, numero_item: int) -> str:
    return f"{_compra_path(ref)}/itens/{numero_item}/resultados"


def url_contrato_detalhe(ref: ContratoRef) -> str:
    """Fallback para contrato sem numeroControlePncpCompra na capa."""
    return _contrato_path(ref)


def url_publica_compra(ref: CompraRef) -> str:
    """Pagina publica da compra, para inspecao manual (gravada em compras.url_pncp)."""
    return f"{PUBLIC_SITE}/{ref.cnpj}/{ref.ano}/{ref.sequencial}"
