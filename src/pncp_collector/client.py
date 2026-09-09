"""Cliente HTTP da API de consulta do PNCP."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Iterator
from datetime import date
from typing import Any

import httpx

from .config import Settings, settings as default_settings

logger = logging.getLogger(__name__)

ATAS_PATH = "/api/consulta/v1/atas"
CONTRATOS_PATH = "/api/consulta/v1/contratos"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class DayFetchError(RuntimeError):
    """Um dia nao pode ser lido apos esgotar as tentativas."""


class PNCPClient:
    """Acesso paginado aos endpoints de consulta. Sem autenticacao."""

    def __init__(self, config: Settings | None = None) -> None:
        self.config = config or default_settings
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

    def _get(self, path: str, params: dict[str, Any]) -> httpx.Response | None:
        """Uma requisicao com retry/backoff. Retorna None em 204 (dia vazio)."""
        for attempt in range(self.config.max_retries):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                self._sleep_backoff(attempt, f"erro de transporte: {exc}")
                continue

            if response.status_code == 204:
                return None
            if response.status_code == 200:
                return response
            if response.status_code in RETRYABLE_STATUS:
                retry_after = response.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else None
                self._sleep_backoff(attempt, f"HTTP {response.status_code}", delay)
                continue

            response.raise_for_status()

        raise DayFetchError(
            f"Falha em {path} com {params} apos {self.config.max_retries} tentativas."
        )

    def _sleep_backoff(self, attempt: int, reason: str, delay: float | None = None) -> None:
        wait = delay if delay is not None else (2**attempt) + random.random()
        wait = min(wait, self.config.max_backoff)
        logger.warning("Retry %d (%s); aguardando %.1fs", attempt + 1, reason, wait)
        time.sleep(wait)

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
            response = self._get(path, params)
            time.sleep(self.config.request_delay)

            if response is None:  # 204: dia sem registros, segue para o proximo
                return

            payload = response.json()
            records = payload.get("data") or []
            if not records:
                return

            yield from records

            # totalPaginas vem em toda pagina; a primeira ja diz onde parar.
            total_pages = payload.get("totalPaginas") or 1
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
