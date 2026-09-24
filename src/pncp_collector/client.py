"""Cliente HTTP das APIs de consulta e de detalhe do PNCP."""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import httpx

from .config import Settings, settings as default_settings
from .ids import (
    AtaRef,
    CompraRef,
    ContratoRef,
    url_arquivos_ata,
    url_arquivos_contrato,
    url_contrato_detalhe,
    url_itens,
    url_resultados,
)

logger = logging.getLogger(__name__)

#: Chamado a cada pagina lida na fase 1: (dia, pagina, total_de_paginas_do_dia).
#: Roda na thread que fez a requisicao — a implementacao precisa ser thread-safe.
PageObserver = Callable[[date, int, int], None]

ATAS_PATH = "/api/consulta/v1/atas"
CONTRATOS_PATH = "/api/consulta/v1/contratos"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
NOT_FOUND_STATUS = {404, 410}


class DayFetchError(RuntimeError):
    """Um dia nao pode ser lido apos esgotar as tentativas."""


class DetailFetchError(RuntimeError):
    """Uma chamada de detalhe falhou: 4xx terminal ou retries esgotados."""

    def __init__(self, path: str, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.path = path
        self.status_code = status_code


class DetailNotFound(DetailFetchError):
    """404/410: o recurso nao existe (ou foi removido) no PNCP."""


class _RetriesExhausted(Exception):
    """Interno: _request esgotou as tentativas. Carrega o ultimo status visto."""

    def __init__(self, status_code: int | None) -> None:
        super().__init__(status_code)
        self.status_code = status_code


class _RateLimiter:
    """Espaca no tempo as requisicoes de todas as threads: no maximo uma a cada
    `interval` segundos, somando todo mundo.

    Sem isso, N threads com um `time.sleep` cada uma dariam um ritmo de N/delay
    — exatamente o que a API de consulta nao aceita.
    """

    def __init__(self, interval: float) -> None:
        self._interval = interval
        self._lock = threading.Lock()
        self._next_at = 0.0

    def acquire(self) -> None:
        """Bloqueia ate ser a vez desta thread e reserva o proximo intervalo."""
        while True:
            with self._lock:
                now = time.monotonic()
                if now >= self._next_at:
                    self._next_at = now + self._interval
                    return
                wait = self._next_at - now
            time.sleep(wait)

    def pause(self, seconds: float) -> None:
        """Segura todas as threads por `seconds`.

        Um 429 nao e problema so de quem o recebeu: com varias requisicoes em
        voo, as outras vao bater na mesma parede. Quem toma o 429 freia o grupo.
        """
        with self._lock:
            self._next_at = max(self._next_at, time.monotonic() + seconds)


class PNCPClient:
    """Acesso aos endpoints de consulta (paginados por dia) e de detalhe. Sem autenticacao.

    Thread-safe: `httpx.Client` pode ser compartilhado. Na consulta (fase 1) o
    ritmo e global, via `_RateLimiter`; no detalhe (fase 2) continua sendo um
    `sleep` por thread, porque aquela API nao limita taxa.
    """

    def __init__(
        self, config: Settings | None = None, on_page: PageObserver | None = None
    ) -> None:
        self.config = config or default_settings
        self._on_page = on_page
        self._limiter = _RateLimiter(self.config.request_delay)
        self._client = httpx.Client(
            base_url=self.config.base_url,
            timeout=self.config.timeout,
            headers={"accept": "*/*", "User-Agent": "pncp-collector/0.1"},
        )

    def __enter__(self) -> "PNCPClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- transporte ----------------------------------------------------------

    def _request(
        self, path: str, params: dict[str, Any] | None = None, *, throttled: bool = False
    ) -> httpx.Response:
        """Uma requisicao com retry/backoff em 429/5xx/transporte.

        Devolve a resposta para qualquer status nao-retentavel (200, 204, 4xx...):
        quem chama decide o que cada um significa. Esgotou: _RetriesExhausted.

        `throttled` sujeita a chamada ao ritmo global (fase 1).
        """
        last_status: int | None = None
        for attempt in range(self.config.max_retries):
            if throttled:
                self._limiter.acquire()
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_status = None
                self._sleep_backoff(attempt, f"erro de transporte: {exc}")
                continue

            if response.status_code in RETRYABLE_STATUS:
                last_status = response.status_code
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else None
                waited = self._sleep_backoff(attempt, f"HTTP {response.status_code}", delay)
                if throttled:
                    self._limiter.pause(waited)
                continue

            return response

        raise _RetriesExhausted(last_status)

    def _sleep_backoff(self, attempt: int, reason: str, delay: float | None = None) -> float:
        wait = delay if delay is not None else (2**attempt) + random.random()
        wait = min(wait, self.config.max_backoff)
        logger.warning("Retry %d (%s); aguardando %.1fs", attempt + 1, reason, wait)
        time.sleep(wait)
        return wait

    # --- consulta (fase 1) ---------------------------------------------------

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response | None:
        """Consulta por dia. Retorna None em 204 (dia vazio); 4xx levanta HTTPStatusError."""
        try:
            response = self._request(path, params, throttled=True)
        except _RetriesExhausted as exc:
            raise DayFetchError(
                f"Falha em {path} com {params} apos {self.config.max_retries} tentativas."
            ) from exc

        if response.status_code == 204:
            return None
        response.raise_for_status()
        return response

    def _iter_day(self, path: str, day: date, extra: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """Percorre todas as paginas de um unico dia (dataInicial == dataFinal)."""
        stamp = day.strftime("%Y%m%d")
        page = 1
        total_pages = 1

        while page <= total_pages:
            params: dict[str, Any] = {
                "dataInicial": stamp,
                "dataFinal": stamp,
                "pagina": page,
                "tamanhoPagina": self.config.page_size,
                **{k: v for k, v in extra.items() if v is not None},
            }
            # O espacamento entre paginas fica com o _RateLimiter, que mede
            # inicio-a-inicio e vale para todas as threads.
            response = self._get(path, params)

            if response is None:  # 204: dia sem registros, segue para o proximo
                return

            payload = response.json()
            records = payload.get("data") or []
            # totalPaginas vem em toda pagina; a primeira ja diz onde parar.
            total_pages = payload.get("totalPaginas") or 1

            # Antes de render os registros, senao o aviso so chegaria depois de
            # quem consome terminar de processar a pagina inteira.
            if self._on_page is not None:
                self._on_page(day, page, total_pages)

            if not records:
                return

            yield from records
            page += 1

    def iter_atas(self, day: date) -> Iterator[dict[str, Any]]:
        """Atas vigentes no dia D (o filtro do endpoint e por vigencia)."""
        return self._iter_day(
            ATAS_PATH,
            day,
            {
                "cnpj": self.config.cnpj,
                "codigoUnidadeAdministrativa": self.config.codigo_unidade_administrativa,
            },
        )

    def iter_contratos(self, day: date) -> Iterator[dict[str, Any]]:
        """Contratos publicados no dia D."""
        return self._iter_day(
            CONTRATOS_PATH,
            day,
            {
                "cnpjOrgao": self.config.cnpj,
                "codigoUnidadeAdministrativa": self.config.codigo_unidade_administrativa,
            },
        )

    # --- detalhe (fase 2) ----------------------------------------------------

    def _get_detail(self, path: str, params: dict[str, Any] | None = None) -> Any:
        """Uma chamada de detalhe. 200 -> JSON; 204 -> None; 404/410 -> DetailNotFound;
        outro 4xx -> DetailFetchError sem retry; retries esgotados -> DetailFetchError."""
        try:
            response = self._request(path, params)
        except _RetriesExhausted as exc:
            raise DetailFetchError(
                path,
                exc.status_code,
                f"{path}: esgotou {self.config.max_retries} tentativas "
                f"(ultimo status {exc.status_code}).",
            ) from exc
        finally:
            time.sleep(self.config.effective_detail_delay)

        if response.status_code == 204:
            return None
        if response.status_code in NOT_FOUND_STATUS:
            raise DetailNotFound(path, response.status_code, f"{path}: HTTP {response.status_code}")
        if response.status_code != 200:
            raise DetailFetchError(
                path, response.status_code, f"{path}: HTTP {response.status_code}"
            )
        return response.json()

    @staticmethod
    def _as_list(payload: Any) -> list[dict[str, Any]]:
        """204 e [] sao equivalentes; objeto unico vira lista de um."""
        if payload is None:
            return []
        if isinstance(payload, dict):
            return [payload]
        return list(payload)

    def get_arquivos_ata(self, ref: AtaRef) -> list[dict[str, Any]]:
        """Lista de arquivos da ata. 404 propaga (ata removida)."""
        return self._as_list(self._get_detail(url_arquivos_ata(ref)))

    def get_arquivos_contrato(self, ref: ContratoRef) -> list[dict[str, Any]]:
        """Lista de arquivos do contrato. 404 propaga (contrato removido)."""
        return self._as_list(self._get_detail(url_arquivos_contrato(ref)))

    def get_itens(self, ref: CompraRef) -> list[dict[str, Any]]:
        """Todos os itens da compra. O endpoint pagina (default 10!) e termina em []."""
        path = url_itens(ref)
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            chunk = self._as_list(
                self._get_detail(
                    path, {"pagina": page, "tamanhoPagina": self.config.detail_page_size}
                )
            )
            items.extend(chunk)
            if len(chunk) < self.config.detail_page_size:
                return items
            page += 1

    def get_resultados(self, ref: CompraRef, numero_item: int) -> list[dict[str, Any]]:
        """Resultados do item. Item sem resultado (204 ou 404) e [] — nao e erro."""
        try:
            return self._as_list(self._get_detail(url_resultados(ref, numero_item)))
        except DetailNotFound:
            return []

    def get_contrato(self, ref: ContratoRef) -> dict[str, Any]:
        """Capa do contrato pela API de detalhe (fallback para compra_key)."""
        payload = self._get_detail(url_contrato_detalhe(ref))
        if not isinstance(payload, dict):
            raise DetailNotFound(url_contrato_detalhe(ref), None, "contrato sem corpo")
        return payload
