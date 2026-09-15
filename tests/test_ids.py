"""Parse dos numeros de controle e URLs de detalhe (strings reais de docs/API_DETALHE.md)."""

import pytest

from pncp_collector.ids import (
    AtaRef,
    CompraRef,
    ContratoRef,
    parse_ata_key,
    parse_compra_key,
    parse_contrato_key,
    url_arquivos_ata,
    url_arquivos_contrato,
    url_contrato_detalhe,
    url_itens,
    url_publica_compra,
    url_resultados,
)

COMPRA = "07954480000179-1-025970/2025"
ATA = "07954480000179-1-025970/2025-000001"
CONTRATO = "07954480000179-2-031999/2026"


def test_parse_compra():
    assert parse_compra_key(COMPRA) == CompraRef("07954480000179", 2025, 25970, COMPRA)


def test_parse_compra_normaliza_espacos():
    assert parse_compra_key(f"  {COMPRA} \n") == CompraRef("07954480000179", 2025, 25970, COMPRA)


def test_parse_ata():
    ref = parse_ata_key(ATA)
    assert ref == AtaRef(parse_compra_key(COMPRA), 1, ATA)
    assert ref.compra.sequencial == 25970


def test_parse_contrato_usa_sequencial_do_contrato():
    ref = parse_contrato_key(CONTRATO)
    assert ref == ContratoRef("07954480000179", 2026, 31999, CONTRATO)
    # O sequencial do contrato nao e o da compra: nao pode virar CompraRef.
    assert parse_compra_key(CONTRATO) is None


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "   ",
        123,
        "07954480000179-1-025970",  # sem barra/ano
        "7954480000179-1-025970/2025",  # cnpj com 13 digitos
        "07954480000179-1-25970/2025",  # sequencial sem zeros a esquerda
        "9-2-3/2026",  # contrato de teste do pipeline: nao e compra
        ATA,  # ata nao e compra
    ],
)
def test_parse_compra_invalido(value):
    assert parse_compra_key(value) is None


@pytest.mark.parametrize("value", [None, "", COMPRA, CONTRATO, "07954480000179-1-025970/2025-1"])
def test_parse_ata_invalido(value):
    assert parse_ata_key(value) is None


@pytest.mark.parametrize("value", [None, "", COMPRA, ATA, "9-2-3/2026"])
def test_parse_contrato_invalido(value):
    assert parse_contrato_key(value) is None


def test_urls_batem_com_os_exemplos_reais():
    compra = parse_compra_key(COMPRA)
    ata = parse_ata_key(ATA)
    contrato = parse_contrato_key(CONTRATO)
    assert (
        url_arquivos_ata(ata)
        == "/api/pncp/v1/orgaos/07954480000179/compras/2025/25970/atas/1/arquivos"
    )
    assert (
        url_arquivos_contrato(contrato)
        == "/api/pncp/v1/orgaos/07954480000179/contratos/2026/31999/arquivos"
    )
    assert url_itens(compra) == "/api/pncp/v1/orgaos/07954480000179/compras/2025/25970/itens"
    assert (
        url_resultados(compra, 4)
        == "/api/pncp/v1/orgaos/07954480000179/compras/2025/25970/itens/4/resultados"
    )
    assert url_contrato_detalhe(contrato) == "/api/pncp/v1/orgaos/07954480000179/contratos/2026/31999"
    assert url_publica_compra(compra) == "https://pncp.gov.br/app/editais/07954480000179/2025/25970"
