"""Тонкий async-клиент info-эндпоинта Hyperliquid (POST /info, JSON).

Повторы допустимы, потому что info-запросы только читают данные. Для торговых действий
(этап 6) повторы запрещены — там после таймаута выполняется сверка, а не повтор.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self

import httpx
import logfire

from app.integrations.hyperliquid.errors import (
    HyperliquidFormatError,
    HyperliquidRateLimitError,
    HyperliquidRequestError,
    HyperliquidUnavailableError,
)
from app.integrations.hyperliquid.rate_limit import (
    IP_WEIGHT_PER_MINUTE,
    TokenBucket,
    extra_weight,
    info_weight,
)

DEFAULT_WEIGHT_BUDGET = IP_WEIGHT_PER_MINUTE // 2


class InfoClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        max_attempts: int = 4,
        backoff_base: float = 0.5,
        bucket: TokenBucket | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            headers={"Content-Type": "application/json"},
        )
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._bucket = bucket or TokenBucket.per_minute(DEFAULT_WEIGHT_BUDGET)
        self._sleep = sleep

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def post(self, payload: dict[str, Any]) -> Any:
        request_type = payload.get("type")
        if not isinstance(request_type, str):
            raise ValueError("payload должен содержать строковый 'type'")
        weight = info_weight(request_type)

        with logfire.span("hyperliquid.info {request_type}", request_type=request_type) as span:
            last_error: Exception | None = None
            for attempt in range(1, self._max_attempts + 1):
                await self._bucket.acquire(weight)
                started = time.monotonic()
                try:
                    response = await self._http.post("/info", json=payload)
                except httpx.TransportError as exc:  # сеть, таймаут
                    last_error = exc
                    retry_reason = type(exc).__name__
                else:
                    span.set_attribute("http.status_code", response.status_code)
                    span.set_attribute("duration_ms", round((time.monotonic() - started) * 1000))
                    if response.status_code == 200:
                        data = self._decode(response, request_type)
                        if isinstance(data, list):
                            self._bucket.charge(extra_weight(request_type, len(data)))
                        span.set_attribute("attempts", attempt)
                        return data
                    if response.status_code == 429:
                        last_error = HyperliquidRateLimitError(f"{request_type}: HTTP 429")
                        retry_reason = "429"
                    elif response.status_code >= 500:
                        last_error = HyperliquidUnavailableError(
                            f"{request_type}: HTTP {response.status_code}"
                        )
                        retry_reason = str(response.status_code)
                    else:
                        raise HyperliquidRequestError(
                            f"{request_type}: HTTP {response.status_code}: {response.text[:200]}",
                            status_code=response.status_code,
                        )

                if attempt < self._max_attempts:
                    delay = self._backoff_base * 2 ** (attempt - 1)
                    if retry_reason == "429":
                        delay *= 4
                    logfire.warn(
                        "hyperliquid.info retry {request_type}",
                        request_type=request_type,
                        attempt=attempt,
                        reason=retry_reason,
                        delay_s=delay,
                    )
                    await self._sleep(delay)

        if isinstance(last_error, HyperliquidRateLimitError):
            raise last_error
        raise HyperliquidUnavailableError(
            f"{request_type}: не удалось после {self._max_attempts} попыток: {last_error!r}"
        ) from last_error

    @staticmethod
    def _decode(response: httpx.Response, request_type: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HyperliquidFormatError(
                f"{request_type}: ответ не JSON: {response.text[:200]!r}"
            ) from exc
