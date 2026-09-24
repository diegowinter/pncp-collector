"""Ritmo global da fase 1: o _RateLimiter e o uso dele na consulta por dia."""

import threading
import time
from datetime import date

import httpx
import pytest

from pncp_collector.client import DayFetchError, PNCPClient, _RateLimiter
from pncp_collector.config import Settings

DAY = date(2026, 9, 8)


def make_client(handler, **overrides):
    config = Settings(
        database_url="postgresql+psycopg://x/y",
        request_delay=0.0,
        max_retries=3,
        page_size=10,
        **overrides,
    )
    instance = PNCPClient(config)
    instance._client = httpx.Client(
        base_url=config.base_url, transport=httpx.MockTransport(handler)
    )
    return instance


def page(records, total_pages=1):
    return httpx.Response(200, json={"data": records, "totalPaginas": total_pages})


# --- o limitador isolado -----------------------------------------------------


def test_limiter_espaca_chamadas_da_mesma_thread():
    limiter = _RateLimiter(0.02)
    started = time.monotonic()
    for _ in range(5):
        limiter.acquire()
    # 5 aquisicoes = 4 intervalos (a primeira e imediata)
    assert time.monotonic() - started >= 0.04 * 1.5


def test_limiter_e_global_entre_threads():
    """O ponto do exercicio: N threads nao viram N vezes o ritmo."""
    limiter = _RateLimiter(0.02)
    stamps: list[float] = []
    lock = threading.Lock()

    def worker():
        limiter.acquire()
        with lock:
            stamps.append(time.monotonic())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    started = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(stamps) == 8
    assert max(stamps) - started >= 0.02 * 7 * 0.9
    stamps.sort()
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert all(gap >= 0.01 for gap in gaps), gaps


def test_limiter_pause_segura_todo_mundo():
    limiter = _RateLimiter(0.0)
    limiter.acquire()
    limiter.pause(0.05)
    started = time.monotonic()
    limiter.acquire()
    assert time.monotonic() - started >= 0.04


# --- a consulta usando o limitador -------------------------------------------


def test_consulta_passa_pelo_limitador_uma_vez_por_pagina(monkeypatch):
    calls = []
    c = make_client(lambda request: page([{"a": 1}], total_pages=3))
    monkeypatch.setattr(c._limiter, "acquire", lambda: calls.append(1))

    assert len(list(c.iter_contratos(DAY))) == 3
    assert len(calls) == 3


def test_429_na_consulta_freia_o_grupo(monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return page([{"a": 1}])

    c = make_client(handler)
    monkeypatch.setattr("pncp_collector.client.time.sleep", lambda s: None)
    paused: list[float] = []
    monkeypatch.setattr(c._limiter, "pause", lambda s: paused.append(s))

    assert len(list(c.iter_contratos(DAY))) == 1
    assert paused == [2.0]


def test_204_continua_sendo_dia_vazio():
    c = make_client(lambda request: httpx.Response(204))
    assert list(c.iter_atas(DAY)) == []


def test_429_ate_esgotar_vira_day_fetch_error(monkeypatch):
    monkeypatch.setattr("pncp_collector.client.time.sleep", lambda s: None)
    c = make_client(lambda request: httpx.Response(429))
    with pytest.raises(DayFetchError):
        list(c.iter_atas(DAY))
