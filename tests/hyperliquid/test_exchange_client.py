from typing import Any

import httpx
import pytest

from app.integrations.hyperliquid.errors import (
    ExchangeNotSentError,
    ExchangeOutcomeUnknownError,
    ExchangeResponseFormatError,
    HyperliquidFormatError,
    HyperliquidRateLimitError,
    HyperliquidRequestError,
)
from app.integrations.hyperliquid.exchange_client import ExchangeClient

PAYLOAD: dict[str, Any] = {
    "action": {"type": "cancel", "cancels": [{"a": 0, "o": 1}]},
    "nonce": 1,
    "signature": {"r": "0x1", "s": "0x2", "v": 27},
    "vaultAddress": None,
    "expiresAfter": None,
}


class Counter:
    def __init__(self, result: httpx.Response | Exception) -> None:
        self.result = result
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def send(result: httpx.Response | Exception) -> tuple[Any, Counter]:
    counter = Counter(result)
    async with ExchangeClient(
        "https://example.invalid", transport=httpx.MockTransport(counter)
    ) as c:
        try:
            return await c.post(PAYLOAD), counter
        except Exception as exc:
            exc.calls = counter.calls  # type: ignore[attr-defined]
            raise


async def test_ok_response_with_statuses() -> None:
    body = {
        "status": "ok",
        "response": {
            "type": "cancel",
            "data": {
                "statuses": [{"error": "Order was never placed, already canceled, or filled."}]
            },
        },
    }
    result, counter = await send(httpx.Response(200, json=body))
    assert result.ok and result.response_type == "cancel"
    assert result.data["statuses"][0]["error"].startswith("Order was never placed")
    assert counter.calls == 1


async def test_ok_response_without_data() -> None:
    result, _ = await send(
        httpx.Response(200, json={"status": "ok", "response": {"type": "default"}})
    )
    assert result.ok and result.response_type == "default" and result.data is None


async def test_err_response() -> None:
    body = {"status": "err", "response": "User or API Wallet 0xabc does not exist."}
    result, _ = await send(httpx.Response(200, json=body))
    assert not result.ok and "does not exist" in (result.error or "")


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (httpx.ConnectError("refused"), ExchangeNotSentError),
        (httpx.ConnectTimeout("connect timeout"), ExchangeNotSentError),
        (httpx.ReadTimeout("read timeout"), ExchangeOutcomeUnknownError),
        (httpx.RemoteProtocolError("dropped"), ExchangeOutcomeUnknownError),
        (httpx.Response(502), ExchangeOutcomeUnknownError),
        (httpx.Response(429), HyperliquidRateLimitError),
        (httpx.Response(422, text="bad"), HyperliquidRequestError),
        (httpx.Response(200, text="<html>"), ExchangeResponseFormatError),
        (httpx.Response(200, json={"weird": True}), ExchangeResponseFormatError),
        (httpx.Response(200, json={"status": "ok", "response": {}}), ExchangeResponseFormatError),
    ],
)
async def test_failures_are_classified_and_never_retried(
    result: httpx.Response | Exception, error: type[Exception]
) -> None:
    with pytest.raises(error) as exc_info:
        await send(result)
    assert exc_info.value.calls == 1  # type: ignore[attr-defined]


def test_format_error_means_outcome_unknown() -> None:
    # Непонятный ответ после отправки: и ошибка формата (стоп новым действиям), и неизвестный исход.
    assert issubclass(ExchangeResponseFormatError, ExchangeOutcomeUnknownError)
    assert issubclass(ExchangeResponseFormatError, HyperliquidFormatError)
