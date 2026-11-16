"""Коллектор рыночных данных (§8 плана): свечи и funding/OI/mark/oracle.

Источники истины:
- закрытые свечи — только REST candleSnapshot (финальные значения биржи);
- WebSocket даёт живую незакрытую свечу, контекст рынка и признак «соединение живо».

Порядок при (пере)подключении (§8.2): подключиться → подписаться → дождаться подтверждения
подписок → восстановить пропуски через REST → только потом состояние healthy.
Все записи идемпотентны, поэтому повтор события или перекрытие REST/WS безопасны.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.domain.market import AssetContext
from app.integrations.hyperliquid import parsing
from app.integrations.hyperliquid.gateway import INTERVALS, HyperliquidGateway
from app.integrations.hyperliquid.ws import (
    Connector,
    WsConnection,
    default_connector,
    iter_messages,
    subscribe_message,
)
from app.market_data.metadata import sync_market

COMPONENT = "collector"


def _utc_now() -> datetime:
    return datetime.now(UTC)


def floor_time(value: datetime, step: timedelta) -> datetime:
    """Округление вниз до границы интервала (от эпохи UTC)."""
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return epoch + ((value - epoch) // step) * step


class WsState(StrEnum):
    STARTING = "starting"
    CONNECTING = "connecting"
    SYNCING = "syncing"
    HEALTHY = "healthy"
    RECONNECTING = "reconnecting"


@dataclass(frozen=True, slots=True)
class CollectorConfig:
    coin: str
    interval: str
    backfill_candles: int = 5000
    rest_sync_interval: float = 60.0
    close_grace: float = 3.0  # пауза после смены свечи перед REST-финализацией
    ctx_sample_interval: float = 60.0
    heartbeat_interval: float = 15.0
    meta_refresh_interval: float = 3600.0
    max_ws_silence: float = 30.0
    ping_interval: float = 20.0
    subscribe_timeout: float = 10.0
    open_candle_write_interval: float = 10.0
    reconnect_initial_delay: float = 1.0
    reconnect_max_delay: float = 30.0


@dataclass
class CollectorHealth:
    ws_state: WsState = WsState.STARTING
    last_ws_message_at: datetime | None = None
    last_rest_sync_at: datetime | None = None
    last_closed_open_time: datetime | None = None
    reconnects: int = 0
    last_error: str | None = None
    last_ctx: AssetContext | None = field(default=None, repr=False)

    def as_details(self, now: datetime) -> dict[str, Any]:
        def age(t: datetime | None) -> float | None:
            return None if t is None else round((now - t).total_seconds(), 1)

        return {
            "ws_state": self.ws_state.value,
            "last_ws_message_age_s": age(self.last_ws_message_at),
            "last_rest_sync_age_s": age(self.last_rest_sync_at),
            "last_closed_open_time": (
                self.last_closed_open_time.isoformat() if self.last_closed_open_time else None
            ),
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


class MarketDataCollector:
    def __init__(
        self,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        ws_url: str,
        config: CollectorConfig,
        *,
        connector: Connector = default_connector,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if config.interval not in INTERVALS:
            raise ValueError(f"неподдерживаемый интервал {config.interval}")
        self._gateway = gateway
        self._sessionmaker = sessionmaker
        self._ws_url = ws_url
        self.config = config
        self._connector = connector
        self._clock = clock
        self._sleep = sleep
        self._step = INTERVALS[config.interval]
        self.health = CollectorHealth()
        self.instance_id = uuid.uuid4().hex[:12]
        self.started_at = clock()
        self.market_id: int | None = None
        self._rollover = asyncio.Event()
        self._open_candle_time: datetime | None = None
        self._open_candle_written_at: datetime | None = None
        self._sync_lock = asyncio.Lock()

    # --- запуск ---

    async def run(self) -> None:
        with logfire.span(
            "collector start {coin} {interval}",
            coin=self.config.coin,
            interval=self.config.interval,
            network=self._gateway.network.value,
        ):
            await self.refresh_meta()
            await self.sync_candles()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._ws_loop())
            tg.create_task(self._rest_sync_loop())
            tg.create_task(
                self._every(self.config.ctx_sample_interval, self.sample_context, immediate=True)
            )
            tg.create_task(
                self._every(self.config.heartbeat_interval, self.heartbeat, immediate=True)
            )
            tg.create_task(self._every(self.config.meta_refresh_interval, self.refresh_meta))

    async def _every(
        self,
        interval: float,
        action: Callable[[], Awaitable[object]],
        *,
        immediate: bool = False,
    ) -> None:
        """Периодическая задача. Ошибка одной итерации не останавливает коллектор."""
        if immediate:
            await self._guarded(action)
        while True:
            await self._sleep(interval)
            await self._guarded(action)

    async def _guarded(self, action: Callable[[], Awaitable[object]]) -> None:
        try:
            await action()
        except Exception as exc:
            self.health.last_error = f"{type(exc).__name__}: {exc}"[:500]
            logfire.exception("collector: ошибка в {action}", action=action.__name__)

    # --- метаданные и свечи (REST) ---

    async def refresh_meta(self) -> None:
        async with self._sessionmaker.begin() as session:
            market_id, _, _ = await sync_market(self._gateway, session, self.config.coin)
        self.market_id = market_id

    def _require_market(self) -> int:
        if self.market_id is None:
            raise RuntimeError("метаданные рынка ещё не загружены")
        return self.market_id

    async def sync_candles(self) -> int:
        """Догружает свечи начиная с последней закрытой в БД (или backfill_candles назад).
        Закрывает пропуски после простоя и финализирует только что закрытые свечи."""
        market_id = self._require_market()
        async with self._sync_lock:
            now = self._clock()
            async with self._sessionmaker() as session:
                last = await market_repo.get_last_closed_candle(
                    session, market_id, self.config.interval
                )
            start = (
                last.open_time + self._step
                if last is not None
                else floor_time(now, self._step) - self._step * self.config.backfill_candles
            )
            candles = await self._gateway.get_candles(
                self.config.coin, self.config.interval, start=start
            )
            async with self._sessionmaker.begin() as session:
                written = await market_repo.upsert_candles(session, market_id, candles)
            closed = [c for c in candles if c.is_closed]
            if closed:
                self.health.last_closed_open_time = closed[-1].open_time
            elif last is not None:
                self.health.last_closed_open_time = last.open_time
            self.health.last_rest_sync_at = now
            if written:
                logfire.info(
                    "collector: свечи синхронизированы ({written} записано, с {start})",
                    written=written,
                    start=start.isoformat(),
                )
            return written

    async def _rest_sync_loop(self) -> None:
        """REST-синхронизация по таймеру или сразу после смены свечи в WebSocket."""
        while True:
            try:
                await asyncio.wait_for(
                    self._rollover.wait(), timeout=self.config.rest_sync_interval
                )
                self._rollover.clear()
                await self._sleep(self.config.close_grace)
            except TimeoutError:
                pass
            await self._guarded(self.sync_candles)

    # --- контекст рынка ---

    async def sample_context(self) -> None:
        """Сохраняет снимок контекста. Если WS-данные устарели — берёт их из REST."""
        market_id = self._require_market()
        now = self._clock()
        ctx = self.health.last_ctx
        max_age = timedelta(seconds=self.config.max_ws_silence)
        if ctx is None or now - ctx.received_at > max_age:
            _, contexts = await self._gateway.get_markets_with_contexts()
            ctx = contexts[self.config.coin]
        async with self._sessionmaker.begin() as session:
            await market_repo.add_context_snapshot(session, market_id, ctx)

    # --- heartbeat ---

    async def heartbeat(self) -> None:
        now = self._clock()
        async with self._sessionmaker.begin() as session:
            await control.beat(
                session,
                component=COMPONENT,
                instance_id=self.instance_id,
                started_at=self.started_at,
                now=now,
                details={
                    **self.health.as_details(now),
                    "coin": self.config.coin,
                    "interval": self.config.interval,
                },
            )

    # --- WebSocket ---

    async def _ws_loop(self) -> None:
        delay = self.config.reconnect_initial_delay
        while True:
            self.health.ws_state = WsState.CONNECTING
            try:
                conn = await self._connector(self._ws_url)
                try:
                    await self._run_connection(conn)
                finally:
                    await conn.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.health.last_error = f"{type(exc).__name__}: {exc}"[:500]
                logfire.warn(
                    "collector: WebSocket переподключение через {delay} с: {error}",
                    delay=delay,
                    error=self.health.last_error,
                )
            else:
                logfire.warn("collector: WebSocket закрыт, переподключение")
            self.health.ws_state = WsState.RECONNECTING
            self.health.reconnects += 1
            await self._sleep(delay)
            if self.health.last_ws_message_at is not None and (
                self._clock() - self.health.last_ws_message_at
            ) < timedelta(seconds=self.config.max_ws_silence):
                delay = self.config.reconnect_initial_delay
            else:
                delay = min(delay * 2, self.config.reconnect_max_delay)

    def _subscriptions(self) -> list[dict[str, str]]:
        return [
            {"type": "candle", "coin": self.config.coin, "interval": self.config.interval},
            {"type": "activeAssetCtx", "coin": self.config.coin},
        ]

    async def _run_connection(self, conn: WsConnection) -> None:
        subscriptions = self._subscriptions()
        for sub in subscriptions:
            await conn.send(subscribe_message(sub))
        messages = iter_messages(
            conn,
            silence_timeout=self.config.max_ws_silence,
            ping_interval=self.config.ping_interval,
        )
        pending = {sub["type"] for sub in subscriptions}

        async def wait_for_acks() -> None:
            async for channel, data in messages:
                if channel == "subscriptionResponse":
                    pending.discard(parsing.parse_subscription_ack(data))
                    if not pending:
                        return
                else:
                    await self._handle(channel, data)

        await asyncio.wait_for(wait_for_acks(), timeout=self.config.subscribe_timeout)

        # Подписки активны: всё, что придёт дальше, не потеряется. Закрываем разрыв через REST.
        self.health.ws_state = WsState.SYNCING
        await self.sync_candles()
        self.health.ws_state = WsState.HEALTHY
        logfire.info("collector: WebSocket healthy")

        async for channel, data in messages:
            await self._handle(channel, data)

    async def _handle(self, channel: str, data: Any) -> None:
        now = self._clock()
        self.health.last_ws_message_at = now
        if channel == "candle":
            candle = parsing.parse_ws_candle(
                data, coin=self.config.coin, interval=self.config.interval, received_at=now
            )
            rollover = (
                self._open_candle_time is not None and candle.open_time > self._open_candle_time
            )
            if rollover:
                self._rollover.set()  # предыдущая свеча закрылась — финализируем через REST
            due = self._open_candle_written_at is None or now - self._open_candle_written_at >= (
                timedelta(seconds=self.config.open_candle_write_interval)
            )
            if rollover or candle.open_time != self._open_candle_time or due:
                async with self._sessionmaker.begin() as session:
                    await market_repo.upsert_candles(session, self._require_market(), [candle])
                self._open_candle_written_at = now
            if self._open_candle_time is None or candle.open_time > self._open_candle_time:
                self._open_candle_time = candle.open_time
        elif channel == "activeAssetCtx":
            self.health.last_ctx = parsing.parse_ws_asset_context(
                data, coin=self.config.coin, received_at=now
            )
        elif channel == "subscriptionResponse":
            pass
        else:
            logfire.warn("collector: неизвестный канал WebSocket {channel}", channel=channel)
