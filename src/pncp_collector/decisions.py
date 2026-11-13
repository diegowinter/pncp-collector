"""Cliente do endpoint de decisoes do OpenRouter (modelos "System One", como o Jev).

POST https://openrouter.ai/api/alpha/decisions
    {"model": ..., "state": {...}, "questions": {nome: {type, instructions, criteria}}}
->  {"answers": {nome: {...}}, "usage": {"cost", "input_tokens", "output_tokens"}, "model", "id"}

Nao e chat: nao ha prompt livre nem texto de saida, so perguntas tipadas
(`choice`, `noul`, `score`) e probabilidades. O endpoint e alfa e o formato pode
mudar; por isso tudo que fala com ele fica neste arquivo.

Thread-safe: `httpx.Client` pode ser compartilhado entre as threads da triagem.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DECISIONS_PATH = "/api/alpha/decisions"
RETRY_STATUSES = {408, 409, 425, 429, 500, 502, 503, 504, 524, 529}


class DecisionsError(RuntimeError):
    """Falha definitiva numa chamada (apos as tentativas)."""

    def __init__(self, status_code: int | None, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class DecisionsResult:
    answers: dict[str, Any]
    model: str | None
    cost_usd: float
    input_tokens: int
    output_tokens: int
    raw: dict[str, Any]


class DecisionsClient:
    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://openrouter.ai",
        model: str = "typesafe/jev-1.14",
        timeout: float = 60.0,
        max_retries: int = 5,
        max_backoff: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("PNCP_OPENROUTER_API_KEY nao definida")
        self.model = model
        self.max_retries = max_retries
        self.max_backoff = max_backoff
        self._stop = threading.Event()
        self._client = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # Identificacao opcional pedida pelo OpenRouter.
                "X-Title": "pncp-collector triagem",
            },
            transport=transport,
        )

    def __enter__(self) -> "DecisionsClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def stop(self) -> None:
        """Ctrl+C: as threads param de esperar backoff e desistem."""
        self._stop.set()

    def close(self) -> None:
        self._stop.set()
        self._client.close()

    def decide(self, state: dict[str, Any], questions: dict[str, Any]) -> DecisionsResult:
        body = {"model": self.model, "state": state, "questions": questions}
        last: tuple[int | None, str] = (None, "sem tentativa")
        for attempt in range(self.max_retries):
            if self._stop.is_set():
                raise DecisionsError(None, "interrompido")
            try:
                response = self._client.post(DECISIONS_PATH, json=body)
            except httpx.TransportError as exc:
                last = (None, f"transporte: {exc}")
                self._backoff(attempt, last[1])
                continue

            if response.status_code == 200:
                return self._parse(response.json())

            message = _error_message(response)
            last = (response.status_code, message)
            if response.status_code in RETRY_STATUSES:
                self._backoff(attempt, f"HTTP {response.status_code}: {message}", response)
                continue
            # 400/401/402/403/413: repetir nao ajuda.
            raise DecisionsError(response.status_code, f"HTTP {response.status_code}: {message}")
        raise DecisionsError(last[0], f"esgotadas {self.max_retries} tentativas; ultima: {last[1]}")

    @staticmethod
    def _parse(payload: dict[str, Any]) -> DecisionsResult:
        answers = payload.get("answers")
        if not isinstance(answers, dict):
            raise DecisionsError(200, f"resposta sem 'answers': {str(payload)[:200]}")
        usage = payload.get("usage") or {}
        return DecisionsResult(
            answers=answers,
            model=payload.get("model"),
            cost_usd=float(usage.get("cost") or 0.0),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            raw=payload,
        )

    def _backoff(self, attempt: int, reason: str, response: httpx.Response | None = None) -> None:
        wait = min(2**attempt + random.uniform(0, 1), self.max_backoff)
        retry_after = response.headers.get("Retry-After") if response is not None else None
        if retry_after:
            try:
                wait = min(max(wait, float(retry_after)), self.max_backoff)
            except ValueError:
                pass
        logger.warning("Decisions: %s; tentativa %d em %.1fs", reason, attempt + 2, wait)
        self._stop.wait(wait)


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
        return str(error.get("message") or response.text)[:300]
    except ValueError:
        return response.text[:300]
