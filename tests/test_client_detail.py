"""Endpoints de detalhe do PNCPClient com transporte falso (sem rede)."""

import httpx
import pytest

from pncp_collector import client as client_module
from pncp_collector.client import DetailFetchError, DetailNotFound, PNCPClient
from pncp_collector.config import Settings
from pncp_collector.ids import parse_ata_key, parse_compra_key, parse_contrato_key

COMPRA = parse_compra_key("07954480000179-1-025970/2025")
ATA = parse_ata_key("07954480000179-1-025970/2025-000001")
CONTRATO = parse_contrato_key("07954480000179-2-031999/2026")


def make_client(handler, **overrides):
    config = Settings(
        database_url="postgresql+psycopg://x/y",
        request_delay=0.0,
        max_retries=3,
        detail_page_size=10,
        **overrides,
    )
    instance = PNCPClient(config)
    instance._client = httpx.Client(
        base_url=config.base_url, transport=httpx.MockTransport(handler)
    )
    return instance


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    calls = []
    monkeypatch.setattr(client_module.time, "sleep", lambda s: calls.append(s))
    return calls


def test_lista_direta_e_objeto_unico_viram_lista():
    def handler(request):
        if request.url.path.endswith("/resultados"):
            return httpx.Response(200, json={"sequencialResultado": 1})
        return httpx.Response(200, json=[{"sequencialDocumento": 1}])

    c = make_client(handler)
    assert c.get_arquivos_ata(ATA) == [{"sequencialDocumento": 1}]
    assert c.get_resultados(COMPRA, 1) == [{"sequencialResultado": 1}]


def test_204_vira_lista_vazia():
    c = make_client(lambda request: httpx.Response(204))
    assert c.get_arquivos_contrato(CONTRATO) == []
    assert c.get_resultados(COMPRA, 3) == []


def test_404_em_resultados_e_vazio_mas_em_itens_e_not_found():
    c = make_client(lambda request: httpx.Response(404, json={"message": "nao cadastrada"}))
    assert c.get_resultados(COMPRA, 1) == []
    with pytest.raises(DetailNotFound) as info:
        c.get_itens(COMPRA)
    assert info.value.status_code == 404
    assert info.value.path.endswith("/itens")
    with pytest.raises(DetailNotFound):
        c.get_arquivos_ata(ATA)
    with pytest.raises(DetailNotFound):
        c.get_contrato(CONTRATO)


def test_itens_pagina_ate_vir_pagina_incompleta():
    seen = []

    def handler(request):
        page = int(request.url.params["pagina"])
        size = int(request.url.params["tamanhoPagina"])
        seen.append((page, size))
        start = (page - 1) * size
        items = [{"numeroItem": n} for n in range(start + 1, min(start + size, 23) + 1)]
        return httpx.Response(200, json=items)

    c = make_client(handler)
    items = c.get_itens(COMPRA)
    assert [i["numeroItem"] for i in items] == list(range(1, 24))
    assert seen == [(1, 10), (2, 10), (3, 10)]


def test_itens_multiplo_exato_faz_uma_chamada_extra_que_volta_vazia():
    def handler(request):
        page = int(request.url.params["pagina"])
        return httpx.Response(200, json=[{"numeroItem": n} for n in range(1, 11)] if page == 1 else [])

    assert len(make_client(handler).get_itens(COMPRA)) == 10


def test_429_duas_vezes_e_depois_200(no_sleep):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json=[{"numeroItem": 1}])

    c = make_client(handler)
    assert c.get_itens(COMPRA) == [{"numeroItem": 1}]
    assert len(attempts) == 3
    # dois backoffs (Retry-After) + o delay pos-chamada
    assert no_sleep[:2] == [2.0, 2.0]


def test_429_ate_esgotar_vira_fetch_error_com_status():
    c = make_client(lambda request: httpx.Response(429))
    with pytest.raises(DetailFetchError) as info:
        c.get_itens(COMPRA)
    assert not isinstance(info.value, DetailNotFound)
    assert info.value.status_code == 429


def test_400_e_terminal_sem_retry():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(400, json={"message": "parametro invalido"})

    c = make_client(handler)
    with pytest.raises(DetailFetchError) as info:
        c.get_arquivos_contrato(CONTRATO)
    assert info.value.status_code == 400
    assert len(attempts) == 1


def test_erro_de_transporte_esgotado():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(DetailFetchError) as info:
        make_client(handler).get_contrato(CONTRATO)
    assert info.value.status_code is None


def test_get_contrato_devolve_dict():
    c = make_client(lambda request: httpx.Response(200, json={"numeroControlePncpCompra": "X"}))
    assert c.get_contrato(CONTRATO)["numeroControlePncpCompra"] == "X"
