import json
from collections.abc import Callable

import httpx
import pytest

from app.integrations.hyperliquid.errors import (
    HyperliquidFormatError,
    HyperliquidRateLimitError,
    HyperliquidRequestError,
    HyperliquidUnavailableError,
)
from app.integrations.hyperliquid.info_client import InfoClient
from app.integrations.hyperliquid.rate_limit import TokenBucket

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = responses
        self.requests: list[dict[str, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_client(
    recorder: Recorder, bucket: TokenBucket | None = None
) -> tuple[InfoClient, list[float]]:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = InfoClient(
        "https://example.invalid",
        transport=httpx.MockTransport(recorder),
        sleep=fake_sleep,
        bucket=bucket,
    )
    return client, sleeps


async def test_success() -> None:
    rec = Recorder([httpx.Response(200, json={"BTC": "65000"})])
    client, sleeps = make_client(rec)
    async with client:
        assert await client.post({"type": "allMids"}) == {"BTC": "65000"}
    assert rec.requests == [{"type": "allMids"}]
    assert sleeps == []


async def test_retries_server_errors_and_network_errors() -> None:
    rec = Recorder(
        [
            httpx.Response(502),
            httpx.ConnectTimeout("timeout"),
            httpx.Response(200, json=[]),
        ]
    )
    client, sleeps = make_client(rec)
    async with client:
        assert await client.post({"type": "meta"}) == []
    assert len(rec.requests) == 3
    assert sleeps == [0.5, 1.0]  # экспоненциальная задержка


async def test_rate_limit_exhausted() -> None:
    rec = Recorder([httpx.Response(429)] * 4)
    client, sleeps = make_client(rec)
    async with client:
        with pytest.raises(HyperliquidRateLimitError):
            await client.post({"type": "meta"})
    assert len(rec.requests) == 4
    assert sleeps == [2.0, 4.0, 8.0]  # для 429 задержка в 4 раза больше


async def test_unavailable_after_retries() -> None:
    rec = Recorder([httpx.Response(500)] * 4)
    client, _ = make_client(rec)
    async with client:
        with pytest.raises(HyperliquidUnavailableError, match="4 попыток"):
            await client.post({"type": "meta"})


async def test_client_errors_are_not_retried() -> None:
    rec = Recorder([httpx.Response(422, text="Failed to deserialize")])
    client, sleeps = make_client(rec)
    async with client:
        with pytest.raises(HyperliquidRequestError, match="422") as exc_info:
            await client.post({"type": "nope"})
    assert exc_info.value.status_code == 422
    assert len(rec.requests) == 1 and sleeps == []


async def test_non_json_response() -> None:
    rec = Recorder([httpx.Response(200, text="<html>")])
    client, _ = make_client(rec)
    async with client:
        with pytest.raises(HyperliquidFormatError, match="не JSON"):
            await client.post({"type": "meta"})


async def test_weight_is_charged_including_items() -> None:
    bucket = TokenBucket(capacity=1000, refill_per_second=0.0001)
    rec = Recorder([httpx.Response(200, json=[{}] * 120), httpx.Response(200, json={})])
    client, _ = make_client(rec, bucket)
    async with client:
        await client.post({"type": "candleSnapshot", "req": {}})  # 20 + 120 // 60
        await client.post({"type": "clearinghouseState", "user": "0x"})  # 2
    assert 1000 - bucket.available == pytest.approx(24, abs=0.01)


async def test_payload_requires_type() -> None:
    client, _ = make_client(Recorder([]))
    async with client:
        with pytest.raises(ValueError, match="type"):
            await client.post({})
