"""Коллектор целиком: фейковая биржа (REST) + фейковый WebSocket + настоящий PostgreSQL."""

import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network
from app.db.models import (
    BookStatsRecord,
    CandleRecord,
    FundingHistoryRecord,
    HeartbeatRecord,
    MarketContextSnapshotRecord,
    TradeFlowRecord,
)
from app.integrations.hyperliquid.gateway import INTERVALS, HyperliquidGateway
from app.integrations.hyperliquid.info_client import InfoClient
from app.integrations.hyperliquid.parsing import datetime_to_ms
from app.market_data.collector import CollectorConfig, MarketDataCollector, WsState

FIXTURES = Path(__file__).parents[1] / "hyperliquid" / "fixtures"
H = timedelta(hours=1)
T0 = datetime(2026, 11, 1, tzinfo=UTC)
CTX = json.loads((FIXTURES / "meta_and_asset_ctxs_mainnet.json").read_text())[1][0]


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def raw_candle(open_time: datetime, close: str = "101", interval: str = "1h") -> dict[str, Any]:
    t = datetime_to_ms(open_time)
    step_ms = int(INTERVALS[interval].total_seconds() * 1000)
    return {
        "t": t,
        "T": t + step_ms - 1,
        "s": "BTC",
        "i": interval,
        "o": "100",
        "c": close,
        "h": "102",
        "l": "99",
        "v": "1.5",
        "n": 10,
    }


class FakeExchange:
    """REST info: свечи до текущего часа включительно (последняя — незакрытая)."""

    def __init__(self, clock: Clock, history_start: datetime) -> None:
        self.clock = clock
        self.history_start = history_start
        self.meta = json.loads((FIXTURES / "meta_testnet.json").read_text())
        self.requests: list[str] = []

    def candles(self, interval: str = "1h") -> list[dict[str, Any]]:
        step = INTERVALS[interval]
        result, t = [], self.history_start
        while t <= self.clock.now:
            result.append(raw_candle(t, interval=interval))
            t += step
        return result

    def __call__(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload["type"])
        if payload["type"] == "meta":
            return httpx.Response(200, json=self.meta)
        if payload["type"] == "metaAndAssetCtxs":
            ctxs = [CTX] * len(self.meta["universe"])
            return httpx.Response(200, json=[self.meta, ctxs])
        if payload["type"] == "candleSnapshot":
            req = payload["req"]
            page = [
                c
                for c in self.candles(req["interval"])
                if req["startTime"] <= c["t"] <= req["endTime"]
            ]
            return httpx.Response(200, json=page)
        if payload["type"] == "fundingHistory":
            page = [f for f in self.funding() if f["time"] >= payload["startTime"]]
            return httpx.Response(200, json=page[:500])
        return httpx.Response(422)

    def funding(self) -> list[dict[str, Any]]:
        result, t = [], self.history_start + H
        while t <= self.clock.now:
            ms = int(t.timestamp() * 1000) + 45
            result.append(
                {"coin": "BTC", "fundingRate": "0.0000125", "premium": "0.0001", "time": ms}
            )
            t += H
        return result


class FakeWs:
    def __init__(self, *, ack: bool = True) -> None:
        self.ack = ack
        self.sent: list[dict[str, Any]] = []
        self.inbox: asyncio.Queue[str | Exception] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str) -> None:
        msg = json.loads(message)
        self.sent.append(msg)
        if self.ack and msg.get("method") == "subscribe":
            ack = {"method": "subscribe", "subscription": msg["subscription"]}
            self.push("subscriptionResponse", ack)

    async def recv(self) -> str | bytes:
        item = await self.inbox.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self) -> None:
        self.closed = True

    def push(self, channel: str, data: Any) -> None:
        self.inbox.put_nowait(json.dumps({"channel": channel, "data": data}))

    def drop(self) -> None:
        self.inbox.put_nowait(ConnectionError("connection lost"))


class FakeConnector:
    def __init__(self, factory: Callable[[], FakeWs]) -> None:
        self.factory = factory
        self.connections: list[FakeWs] = []

    async def __call__(self, url: str) -> FakeWs:
        ws = self.factory()
        self.connections.append(ws)
        return ws


FAST = CollectorConfig(
    coin="BTC",
    interval="1h",
    backfill_candles=24,
    rest_sync_interval=100.0,  # таймер не мешает: синхронизацию в тестах вызывает смена свечи
    close_grace=0.0,
    ctx_sample_interval=100.0,
    heartbeat_interval=100.0,
    meta_refresh_interval=100.0,
    max_ws_silence=0.3,
    ping_interval=100.0,
    subscribe_timeout=1.0,
    open_candle_write_interval=0.0,
    reconnect_initial_delay=0.01,
    reconnect_max_delay=0.05,
)


def make_collector(
    sm: async_sessionmaker[AsyncSession],
    exchange: FakeExchange,
    connector: FakeConnector | None = None,
    config: CollectorConfig = FAST,
) -> MarketDataCollector:
    client = InfoClient("https://example.invalid", transport=httpx.MockTransport(exchange))
    gateway = HyperliquidGateway(
        client, Network.TESTNET, clock=exchange.clock, close_grace=timedelta(0)
    )
    return MarketDataCollector(
        gateway,
        sm,
        "wss://example.invalid/ws",
        config,
        connector=connector or FakeConnector(FakeWs),
        clock=exchange.clock,
    )


async def eventually(check: Callable[[], Awaitable[bool]], limit: float = 3.0) -> None:
    try:
        async with asyncio.timeout(limit):
            while not await check():  # noqa: ASYNC110 — опрос состояния в тестах
                await asyncio.sleep(0.02)
    except TimeoutError:
        raise AssertionError("условие не выполнилось вовремя") from None


async def count(sm: async_sessionmaker[AsyncSession], model: Any, *where: Any) -> int:
    async with sm() as s:
        return (await s.execute(select(func.count()).select_from(model).where(*where))).scalar_one()


async def closed_count(sm: async_sessionmaker[AsyncSession]) -> int:
    return await count(sm, CandleRecord, CandleRecord.is_closed.is_(True))


async def test_backfill_then_incremental_sync(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 30 * H + timedelta(minutes=20))
    collector = make_collector(sm, FakeExchange(clock, history_start=T0))

    await collector.refresh_meta()
    await collector.sync_candles()
    # backfill_candles=24: от now-24h; последняя свеча (текущий час) незакрыта.
    assert await closed_count(sm) == 24
    assert await count(sm, CandleRecord) == 25

    clock.now += H  # прошёл час: предыдущая свеча закрылась, появилась новая
    await collector.sync_candles()
    assert await closed_count(sm) == 25
    assert collector.health.last_closed_open_time == T0 + 30 * H

    await collector.sync_candles()  # повтор ничего не дублирует
    assert await count(sm, CandleRecord) == 26


async def test_funding_backfill_then_incremental(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 30 * H + timedelta(minutes=20))
    collector = make_collector(sm, FakeExchange(clock, history_start=T0))
    await collector.refresh_meta()

    # backfill_candles=24: funding с начала окна backfill свечей (T0+6h) — 25 начислений
    assert await collector.sync_funding() == 25
    assert collector.health.last_funding_time == T0 + 30 * H + timedelta(milliseconds=45)
    assert await collector.sync_funding() == 0  # ничего нового — ничего не пишется

    clock.now += 2 * H
    assert await collector.sync_funding() == 2
    assert await count(sm, FundingHistoryRecord) == 27


async def test_websocket_lifecycle_rollover_and_reconnect(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 10 * H + timedelta(minutes=5))
    exchange = FakeExchange(clock, history_start=T0)
    connector = FakeConnector(FakeWs)
    collector = make_collector(sm, exchange, connector)
    task = asyncio.create_task(collector.run())
    try:
        await eventually(lambda: _true(collector.health.ws_state is WsState.HEALTHY))
        # Heartbeat и снимок контекста пишутся сразу при старте, не через интервал.
        # (интервалы в FAST — 100 с, так что запись могла появиться только от немедленного запуска)
        await eventually(lambda: _count_is(sm, HeartbeatRecord, 1))
        await eventually(lambda: _count_is(sm, MarketContextSnapshotRecord, 1))
        ws = connector.connections[0]
        assert [m["subscription"]["type"] for m in ws.sent] == ["candle", "activeAssetCtx"]
        assert await closed_count(sm) == 10

        # Живая свеча из WebSocket пишется как незакрытая; дубликаты безопасны.
        ws.push("candle", raw_candle(T0 + 10 * H, close="101.5"))
        ws.push("candle", raw_candle(T0 + 10 * H, close="101.5"))
        ws.push("activeAssetCtx", {"coin": "BTC", "ctx": CTX})
        await eventually(lambda: _true(collector.health.last_ctx is not None))
        await eventually(
            lambda: _candle_close_is(sm, T0 + 10 * H, "101.5"),
        )

        # Смена часа: WS присылает новую свечу → сразу REST-финализация предыдущей.
        clock.now += H
        ws.push("candle", raw_candle(T0 + 11 * H))
        await eventually(lambda: _closed_is(sm, 11))

        # Разрыв на 3 часа: после переподключения пропуски закрываются через REST.
        ws.drop()
        clock.now += 3 * H
        await eventually(lambda: _true(len(connector.connections) == 2))
        await eventually(lambda: _true(collector.health.ws_state is WsState.HEALTHY))
        assert await closed_count(sm) == 14
        assert collector.health.reconnects == 1
        assert ws.closed
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_silence_triggers_reconnect(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 5 * H)
    connector = FakeConnector(FakeWs)
    collector = make_collector(sm, FakeExchange(clock, history_start=T0), connector)
    task = asyncio.create_task(collector.run())
    try:
        # После подтверждений подписок сообщений нет → через max_ws_silence переподключение.
        await eventually(lambda: _true(len(connector.connections) >= 3))
        assert "нет сообщений" in (collector.health.last_error or "")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_missing_subscription_ack_triggers_reconnect(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 5 * H)
    connector = FakeConnector(lambda: FakeWs(ack=False))
    collector = make_collector(sm, FakeExchange(clock, history_start=T0), connector)
    task = asyncio.create_task(collector.run())
    try:
        await eventually(lambda: _true(len(connector.connections) >= 2))
        assert collector.health.ws_state is not WsState.HEALTHY
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_context_sampling_and_heartbeat(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 5 * H)
    exchange = FakeExchange(clock, history_start=T0)
    collector = make_collector(sm, exchange)
    await collector.refresh_meta()

    # Нет свежих WS-данных → снимок из REST.
    await collector.sample_context()
    async with sm() as s:
        [snapshot] = (await s.execute(select(MarketContextSnapshotRecord))).scalars()
    assert snapshot.source == "rest"
    assert "metaAndAssetCtxs" in exchange.requests

    await collector.heartbeat()
    async with sm() as s:
        [hb] = (await s.execute(select(HeartbeatRecord))).scalars()
    assert hb.component == "collector"
    assert hb.details["coin"] == "BTC" and hb.details["ws_state"] == "starting"


async def _true(value: bool) -> bool:
    return value


async def _count_is(sm: async_sessionmaker[AsyncSession], model: Any, expected: int) -> bool:
    return await count(sm, model) == expected


async def _closed_is(sm: async_sessionmaker[AsyncSession], expected: int) -> bool:
    return await closed_count(sm) == expected


async def _candle_close_is(
    sm: async_sessionmaker[AsyncSession], open_time: datetime, close: str
) -> bool:
    async with sm() as s:
        stmt = select(CandleRecord).where(CandleRecord.open_time == open_time)
        record = (await s.execute(stmt)).scalar_one_or_none()
    return record is not None and str(record.close) == close and record.source == "ws"


# --- этап 30m: свечи 30m, поток сделок и стакан ---


def ws_trade(
    t: datetime, tid: int, side: str = "B", px: str = "65000", sz: str = "0.5"
) -> dict[str, Any]:
    return {
        "coin": "BTC",
        "side": side,
        "px": px,
        "sz": sz,
        "time": datetime_to_ms(t),
        "tid": tid,
        "hash": "0x0",
        "users": [],
    }


def ws_book(t: datetime, bid_sz: str = "3", ask_sz: str = "1") -> dict[str, Any]:
    def side(start: int, step: int, sz: str) -> list[dict[str, Any]]:
        return [{"px": str(start + step * i), "sz": sz, "n": 1} for i in range(20)]

    return {
        "coin": "BTC",
        "time": datetime_to_ms(t),
        "levels": [side(64999, -1, bid_sz), side(65001, 1, ask_sz)],
    }


FLOW = dataclasses.replace(
    FAST, extra_intervals=("30m",), flow_interval="30m", book_sample_seconds=10
)


async def test_extra_30m_candles_synced(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 10 * H + timedelta(minutes=40))
    collector = make_collector(sm, FakeExchange(clock, history_start=T0), config=FLOW)
    await collector.refresh_meta()
    await collector.sync_extra_candles()
    closed_30m = await count(
        sm, CandleRecord, CandleRecord.interval == "30m", CandleRecord.is_closed.is_(True)
    )
    assert closed_30m == 21  # 10.5 ч закрытых 30-минутных свечей от T0
    clock.now += timedelta(minutes=30)
    await collector.sync_extra_candles()
    assert (
        await count(
            sm, CandleRecord, CandleRecord.interval == "30m", CandleRecord.is_closed.is_(True)
        )
        == 22
    )


async def test_flow_and_book_buckets_written(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    start = T0 + 10 * H + timedelta(minutes=29)  # сбор начат за минуту до корзины 10:30
    clock = Clock(start)
    collector = make_collector(sm, FakeExchange(clock, history_start=T0), config=FLOW)
    await collector.refresh_meta()
    b0 = T0 + 10 * H  # корзина 10:00–10:30 — неполная (сбор начат посередине)
    b1 = b0 + timedelta(minutes=30)
    await collector._handle(
        "trades", [ws_trade(start - timedelta(minutes=5), 1)]
    )  # до старта — нет
    await collector._handle("trades", [ws_trade(start + timedelta(seconds=1), 2)])
    await collector._handle(
        "trades",
        [
            ws_trade(b1 + timedelta(minutes=1), 3, "B", sz="1"),
            ws_trade(b1 + timedelta(minutes=2), 4, "A", sz="0.25"),
            ws_trade(b1 + timedelta(minutes=1), 3, "B", sz="1"),
        ],
    )  # дубль tid
    await collector._handle("l2Book", ws_book(b1 + timedelta(minutes=1)))
    await collector._handle(
        "l2Book", ws_book(b1 + timedelta(minutes=1, seconds=3))
    )  # чаще 10 с — пропуск
    await collector._handle("l2Book", ws_book(b1 + timedelta(minutes=2), bid_sz="1", ask_sz="1"))

    clock.now = b1 + timedelta(minutes=10)
    assert await collector.flush_flow() == 1  # закрыта только корзина 10:00 (поток)
    clock.now = b1 + timedelta(minutes=31)
    await collector.flush_flow()

    async with sm() as session:
        flows = {r.open_time: r for r in (await session.execute(select(TradeFlowRecord))).scalars()}
        books = {r.open_time: r for r in (await session.execute(select(BookStatsRecord))).scalars()}
    assert not flows[b0].complete and flows[b0].buy_count == 1
    full = flows[b1]
    assert full.complete and full.buy_count == 1 and full.sell_count == 1
    assert full.buy_volume == Decimal("1") and full.sell_volume == Decimal("0.25")
    assert full.buy_notional == Decimal("65000")
    book = books[b1]
    assert book.complete and book.samples == 2
    assert book.imbalance_top5_mean == pytest.approx((0.5 + 0.0) / 2)  # (3-1)/(3+1)=0.5 и 0
    assert collector.health.last_flow_bucket == b1 and collector.health.last_flow_complete


async def test_ws_gap_marks_buckets_incomplete(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    sm = committing_sessionmaker
    clock = Clock(T0 + 10 * H - timedelta(minutes=1))
    collector = make_collector(sm, FakeExchange(clock, history_start=T0), config=FLOW)
    await collector.refresh_meta()
    b = T0 + 10 * H
    await collector._handle("trades", [ws_trade(b + timedelta(minutes=1), 1)])
    assert collector._flow is not None
    collector._flow.mark_gap(b + timedelta(minutes=5), b + timedelta(minutes=6))  # обрыв WS
    await collector._handle("trades", [ws_trade(b + timedelta(minutes=7), 2)])
    clock.now = b + timedelta(minutes=31)
    await collector.flush_flow()
    async with sm() as session:
        row = (await session.execute(select(TradeFlowRecord))).scalar_one()
    assert not row.complete and row.buy_count == 2
