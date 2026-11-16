import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.config.settings import Network
from app.integrations.hyperliquid.errors import HyperliquidFormatError, UnknownMarketError
from app.integrations.hyperliquid.gateway import (
    FILLS_PAGE_LIMIT,
    HyperliquidGateway,
    normalize_address,
)
from app.integrations.hyperliquid.info_client import InfoClient
from app.integrations.hyperliquid.parsing import datetime_to_ms

FIXTURES = Path(__file__).parent / "fixtures"
USER = "0x" + "Ab" * 20
H = timedelta(hours=1)
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def raw_candle(open_time: datetime, coin: str = "BTC") -> dict[str, Any]:
    t = datetime_to_ms(open_time)
    return {
        "t": t,
        "T": t + 3_599_999,
        "s": coin,
        "i": "1h",
        "o": "100",
        "c": "101",
        "h": "102",
        "l": "99",
        "v": "1.5",
        "n": 10,
    }


def raw_fill(tid: int, time: datetime) -> dict[str, Any]:
    fill: dict[str, Any] = load("user_fills.json")[0]
    fill.update(tid=tid, time=datetime_to_ms(time))
    return fill


class FakeExchange:
    """Отвечает на info-запросы через обработчик и запоминает payload."""

    def __init__(self, handler: Any) -> None:
        self.handler = handler
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.payloads.append(payload)
        return httpx.Response(200, json=self.handler(payload))


def make_gateway(exchange: FakeExchange, now: datetime) -> HyperliquidGateway:
    client = InfoClient("https://example.invalid", transport=httpx.MockTransport(exchange))
    return HyperliquidGateway(client, Network.TESTNET, clock=lambda: now)


async def test_get_market_and_unknown_market() -> None:
    gw = make_gateway(FakeExchange(lambda p: load("meta_testnet.json")), T0)
    btc = await gw.get_market("BTC")
    assert btc.asset_id == 3
    with pytest.raises(UnknownMarketError):
        await gw.get_market("NOPE")


async def test_candles_paginate_and_respect_window() -> None:
    all_candles = [raw_candle(T0 + i * H) for i in range(12)]

    def handler(payload: dict[str, Any]) -> list[dict[str, Any]]:
        req = payload["req"]
        page = [c for c in all_candles if req["startTime"] <= c["t"] <= req["endTime"]]
        return page[:5]  # биржа отдаёт ограниченную страницу

    exchange = FakeExchange(handler)
    now = T0 + 20 * H
    gw = make_gateway(exchange, now)
    candles = await gw.get_candles("BTC", "1h", start=T0 + H, end=T0 + 11 * H)

    assert [c.open_time for c in candles] == [T0 + i * H for i in range(1, 11)]
    assert all(c.is_closed for c in candles)
    assert len(exchange.payloads) == 2  # 5 + 5, после конца окна запросов больше нет


async def test_candles_reject_unknown_interval() -> None:
    gw = make_gateway(FakeExchange(lambda p: []), T0)
    with pytest.raises(ValueError, match="интервал"):
        await gw.get_candles("BTC", "7m", start=T0)


async def test_fills_paginate_and_deduplicate() -> None:
    # Первая страница полная; последние fills в ту же миллисекунду, что и начало второй.
    page1 = [raw_fill(i, T0 + timedelta(milliseconds=i)) for i in range(FILLS_PAGE_LIMIT)]
    last_ms = T0 + timedelta(milliseconds=FILLS_PAGE_LIMIT - 1)
    page2 = [raw_fill(FILLS_PAGE_LIMIT - 1, last_ms), raw_fill(10_000, last_ms)]
    pages = iter([page1, page2])
    exchange = FakeExchange(lambda p: next(pages))
    gw = make_gateway(exchange, T0)

    fills = await gw.get_fills(USER, start=T0)
    assert len(fills) == FILLS_PAGE_LIMIT + 1
    assert len({f.tid for f in fills}) == len(fills)
    assert exchange.payloads[1]["startTime"] == datetime_to_ms(last_ms)
    assert exchange.payloads[0]["user"] == USER.lower()


async def test_fills_pagination_stuck_raises() -> None:
    same_ms = [raw_fill(i, T0) for i in range(FILLS_PAGE_LIMIT)]
    gw = make_gateway(FakeExchange(lambda p: same_ms), T0)
    with pytest.raises(HyperliquidFormatError, match="не продвигается"):
        await gw.get_fills(USER, start=T0)


async def test_account_requests_use_normalized_address() -> None:
    responses = {
        "clearinghouseState": load("clearinghouse_state.json"),
        "frontendOpenOrders": load("frontend_open_orders.json"),
        "orderStatus": load("order_status_unknown.json"),
        "userRateLimit": load("user_rate_limit.json"),
    }
    exchange = FakeExchange(lambda p: responses[p["type"]])
    gw = make_gateway(exchange, T0)

    assert (await gw.get_account_state(USER)).positions
    assert len(await gw.get_open_orders(USER)) == 2
    assert not (await gw.get_order_status(USER, "0x" + "0" * 32)).found
    assert (await gw.get_user_rate_limit(USER)).requests_cap > 0
    assert {p["user"] for p in exchange.payloads} == {USER.lower()}


@pytest.mark.parametrize("bad", ["", "0x123", "ab" * 20, "0x" + "zz" * 20])
def test_invalid_address_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="адрес"):
        normalize_address(bad)


async def test_invalid_cloid_rejected() -> None:
    gw = make_gateway(FakeExchange(lambda p: {}), T0)
    with pytest.raises(ValueError, match="cloid"):
        await gw.get_order_status(USER, "0x123")
