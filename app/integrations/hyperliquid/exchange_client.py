"""Клиент POST /exchange. Повторов нет: торговое действие нельзя слепо отправлять дважды.

Исходы:
- ExchangeResponse(ok=True)  — биржа приняла запрос (статусы отдельных ордеров — внутри data);
- ExchangeResponse(ok=False) — биржа отклонила запрос целиком ({"status": "err"});
- ExchangeNotSentError       — соединение не установлено, запрос точно не отправлен;
- ExchangeOutcomeUnknownError — исход неизвестен, нужна сверка.
"""

from dataclasses import dataclass
from types import TracebackType
from typing import Any, Self

import httpx
import logfire

from app.integrations.hyperliquid.actions import action_weight
from app.integrations.hyperliquid.errors import (
    ExchangeNotSentError,
    ExchangeOutcomeUnknownError,
    ExchangeResponseFormatError,
    HyperliquidRateLimitError,
    HyperliquidRequestError,
)
from app.integrations.hyperliquid.rate_limit import TokenBucket

DEFAULT_EXCHANGE_WEIGHT_BUDGET = 300


@dataclass(frozen=True, slots=True)
class ExchangeResponse:
    ok: bool
    response_type: str | None
    data: Any
    error: str | None
    raw: Any


def parse_exchange_response(body: Any) -> ExchangeResponse:
    if not isinstance(body, dict) or body.get("status") not in ("ok", "err"):
        raise ExchangeResponseFormatError(
            f"/exchange: неизвестный формат ответа: {str(body)[:300]}"
        )
    if body["status"] == "err":
        return ExchangeResponse(
            ok=False, response_type=None, data=None, error=str(body.get("response")), raw=body
        )
    response = body.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("type"), str):
        raise ExchangeResponseFormatError(f"/exchange: нет response.type: {str(body)[:300]}")
    return ExchangeResponse(
        ok=True, response_type=response["type"], data=response.get("data"), error=None, raw=body
    )


class ExchangeClient:
    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        bucket: TokenBucket | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout, transport=transport)
        self._bucket = bucket or TokenBucket.per_minute(DEFAULT_EXCHANGE_WEIGHT_BUDGET)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._http.aclose()

    async def post(self, payload: dict[str, Any]) -> ExchangeResponse:
        action = payload["action"]
        action_type = action.get("type")
        await self._bucket.acquire(action_weight(action))
        # В лог — только тип действия и nonce, без подписи и содержимого.
        with logfire.span(
            "hyperliquid.exchange {action_type}", action_type=action_type, nonce=payload["nonce"]
        ) as span:
            try:
                response = await self._http.post("/exchange", json=payload)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                raise ExchangeNotSentError(
                    f"{action_type}: соединение не установлено: {exc!r}"
                ) from exc
            except httpx.TransportError as exc:
                raise ExchangeOutcomeUnknownError(
                    f"{action_type}: ответ не получен ({type(exc).__name__}), исход неизвестен"
                ) from exc

            span.set_attribute("http.status_code", response.status_code)
            if response.status_code == 429:
                raise HyperliquidRateLimitError(f"{action_type}: HTTP 429 — запрос отклонён")
            if response.status_code >= 500:
                raise ExchangeOutcomeUnknownError(
                    f"{action_type}: HTTP {response.status_code}, исход неизвестен"
                )
            if response.status_code != 200:
                raise HyperliquidRequestError(
                    f"{action_type}: HTTP {response.status_code}: {response.text[:200]}",
                    status_code=response.status_code,
                )
            try:
                body = response.json()
            except ValueError as exc:
                raise ExchangeResponseFormatError(
                    f"{action_type}: ответ не JSON: {response.text[:200]!r}"
                ) from exc
            result = parse_exchange_response(body)
            span.set_attribute("exchange.ok", result.ok)
            if result.response_type:
                span.set_attribute("exchange.response_type", result.response_type)
            return result
