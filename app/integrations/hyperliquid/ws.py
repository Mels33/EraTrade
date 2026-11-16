"""WebSocket Hyperliquid: подписки, ping, обнаружение тишины.

Наблюдения на mainnet (2026-11-17):
- подписка на несуществующий инструмент обрывает всё соединение без close frame,
  поэтому инструмент обязательно проверяется по актуальному meta до подписки;
- candle приходит одиночным объектом (~1.5 сообщения/с), activeAssetCtx — ~1/с;
- {"method": "ping"} → {"channel": "pong"}.
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import websockets

from app.integrations.hyperliquid.errors import HyperliquidError
from app.integrations.hyperliquid.parsing import parse_ws_message


class WsConnection(Protocol):
    async def send(self, message: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self) -> None: ...


Connector = Callable[[str], Awaitable[WsConnection]]


async def default_connector(url: str) -> WsConnection:
    return await websockets.connect(url, open_timeout=10, close_timeout=5, max_queue=1024)


class WsStaleError(HyperliquidError):
    """Нет сообщений дольше допустимого — соединение считается мёртвым."""


class WsServerError(HyperliquidError):
    """Сервер прислал channel=error."""


def subscribe_message(subscription: dict[str, Any]) -> str:
    return json.dumps({"method": "subscribe", "subscription": subscription})


PING_MESSAGE = json.dumps({"method": "ping"})


async def iter_messages(
    conn: WsConnection,
    *,
    silence_timeout: float,
    ping_interval: float,
    clock: Callable[[], float] = time.monotonic,
) -> AsyncIterator[tuple[str, Any]]:
    """Сообщения (channel, data). Шлёт ping, бросает WsStaleError при тишине и WsServerError
    на channel=error. pong отфильтровывается, но продлевает жизнь соединения."""
    last_ping = clock()
    while True:
        if clock() - last_ping >= ping_interval:
            await conn.send(PING_MESSAGE)
            last_ping = clock()
        try:
            raw = await asyncio.wait_for(conn.recv(), timeout=silence_timeout)
        except TimeoutError as exc:
            raise WsStaleError(f"нет сообщений дольше {silence_timeout} с") from exc
        channel, data = parse_ws_message(raw)
        if channel == "pong":
            continue
        if channel == "error":
            raise WsServerError(f"ошибка сервера WebSocket: {str(data)[:200]}")
        yield channel, data
