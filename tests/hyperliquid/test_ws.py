import asyncio
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from app.domain.market import DataSource
from app.integrations.hyperliquid import parsing
from app.integrations.hyperliquid.errors import HyperliquidFormatError
from app.integrations.hyperliquid.ws import (
    PING_MESSAGE,
    WsServerError,
    WsStaleError,
    iter_messages,
    subscribe_message,
)

NOW = datetime(2026, 11, 17, tzinfo=UTC)
CANDLE = {
    "t": 1794862800000,
    "T": 1794866399999,
    "s": "BTC",
    "i": "1h",
    "o": "65033.0",
    "c": "65175.0",
    "h": "65273.0",
    "l": "64922.0",
    "v": "816.14163",
    "n": 8325,
}


class ScriptedWs:
    def __init__(self, messages: list[dict[str, Any]], *, hang: bool = False) -> None:
        self.messages = [json.dumps(m) for m in messages]
        self.hang = hang
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        if self.messages:
            return self.messages.pop(0)
        if self.hang:
            await asyncio.sleep(10)
        raise ConnectionError("closed")

    async def close(self) -> None:
        pass


async def collect(ws: ScriptedWs, **kwargs: Any) -> list[tuple[str, Any]]:
    result = []
    async for item in iter_messages(ws, **kwargs):
        result.append(item)
    return result


async def test_pong_filtered_and_messages_yielded() -> None:
    ws = ScriptedWs([{"channel": "pong"}, {"channel": "candle", "data": CANDLE}])
    with pytest.raises(ConnectionError):
        await collect(ws, silence_timeout=1, ping_interval=100)
    # До разрыва успели получить только candle (pong отфильтрован).
    gen = iter_messages(
        ScriptedWs([{"channel": "pong"}, {"channel": "candle", "data": CANDLE}]),
        silence_timeout=1,
        ping_interval=100,
    )
    assert await anext(gen) == ("candle", CANDLE)


async def test_silence_raises_stale() -> None:
    with pytest.raises(WsStaleError):
        await collect(ScriptedWs([], hang=True), silence_timeout=0.05, ping_interval=100)


async def test_server_error_channel_raises() -> None:
    ws = ScriptedWs([{"channel": "error", "data": "Invalid subscription"}])
    with pytest.raises(WsServerError, match="Invalid subscription"):
        await collect(ws, silence_timeout=1, ping_interval=100)


async def test_ping_sent_when_due() -> None:
    times = iter([0.0, 0.0, 25.0, 25.0, 26.0])
    ws = ScriptedWs([{"channel": "candle", "data": CANDLE}, {"channel": "candle", "data": CANDLE}])
    gen = iter_messages(ws, silence_timeout=1, ping_interval=20, clock=lambda: next(times))
    await anext(gen)
    await anext(gen)
    assert ws.sent == [PING_MESSAGE]


def test_subscribe_message_format() -> None:
    assert json.loads(subscribe_message({"type": "candle", "coin": "BTC", "interval": "1h"})) == {
        "method": "subscribe",
        "subscription": {"type": "candle", "coin": "BTC", "interval": "1h"},
    }


def test_ws_candle_is_never_closed() -> None:
    candle = parsing.parse_ws_candle(CANDLE, coin="BTC", interval="1h", received_at=NOW)
    assert not candle.is_closed
    assert candle.source is DataSource.WS
    with pytest.raises(HyperliquidFormatError, match="чужая свеча"):
        parsing.parse_ws_candle(CANDLE, coin="ETH", interval="1h", received_at=NOW)


def test_ws_asset_context() -> None:
    data = {
        "coin": "BTC",
        "ctx": {
            "funding": "0.0000073742",
            "openInterest": "37142.8255200001",
            "oraclePx": "65203.0",
            "markPx": "65174.0",
            "midPx": "65174.5",
            "impactPxs": ["65174.0", "65175.0"],
        },
    }
    ctx = parsing.parse_ws_asset_context(data, coin="BTC", received_at=NOW)
    assert ctx.source is DataSource.WS and ctx.mid_price is not None
    with pytest.raises(HyperliquidFormatError, match="чужой инструмент"):
        parsing.parse_ws_asset_context(data, coin="ETH", received_at=NOW)


def test_subscription_ack_and_bad_json() -> None:
    ack = {"method": "subscribe", "subscription": {"type": "candle", "coin": "BTC"}}
    assert parsing.parse_subscription_ack(ack) == "candle"
    with pytest.raises(HyperliquidFormatError, match="не JSON"):
        parsing.parse_ws_message("not json")
