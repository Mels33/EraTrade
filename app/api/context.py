"""Зависимости API: БД, шлюз биржи, risk engine, фабрика торгового движка.

Торговый движок (ключ API-кошелька, блокировка единственного процесса) открывается только
на время команды cancel-all/flatten/kill — так же, как в CLI. Команды открытия позиции в
API нет: даже с токеном оператора через панель можно только остановить и закрыть.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.config.strategy import StrategyConfig, load_strategy
from app.db.session import create_engine, create_sessionmaker
from app.execution.engine import ExecutionEngine
from app.execution.session import open_trading_session
from app.integrations.hyperliquid.gateway import HyperliquidGateway, open_gateway
from app.market_data.metadata import sync_market
from app.observability.notify import Notifier, build_notifier
from app.risk.engine import RiskEngine

EngineFactory = Callable[[], AbstractAsyncContextManager[ExecutionEngine]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


class TtlCache:
    """Кэш живых данных биржи на несколько секунд: панель опрашивает часто, лимит запросов общий."""

    def __init__(self, ttl: float) -> None:
        self.ttl = ttl
        self._values: dict[str, tuple[float, Any]] = {}
        self._lock = asyncio.Lock()

    async def get(self, key: str, load: Callable[[], Awaitable[Any]]) -> Any:
        async with self._lock:
            cached = self._values.get(key)
            if cached and time.monotonic() - cached[0] < self.ttl:
                return cached[1]
            value = await load()
            self._values[key] = (time.monotonic(), value)
            return value

    def clear(self) -> None:
        self._values.clear()


@dataclass
class ApiContext:
    settings: Settings
    sm: async_sessionmaker[AsyncSession]
    gateway: HyperliquidGateway
    strategy: StrategyConfig
    market_id: int
    coin: str
    user: str
    risk: RiskEngine
    notifier: Notifier
    engine_factory: EngineFactory | None  # None — ключа нет, торговые команды недоступны
    clock: Callable[[], datetime] = _utc_now
    cache: TtlCache = field(default_factory=lambda: TtlCache(5.0))


@asynccontextmanager
async def build_context(settings: Settings) -> AsyncIterator[ApiContext]:
    if settings.api_token is None:
        raise RuntimeError("не задан API_TOKEN — без него API не запускается")
    if settings.hl_account_address is None:
        raise RuntimeError("не задан HL_ACCOUNT_ADDRESS")
    strategy = load_strategy(settings.strategy_config_path)
    db_engine = create_engine(settings)
    sm = create_sessionmaker(db_engine)
    notifier = build_notifier(settings)
    try:
        async with open_gateway(settings, service="trading") as gateway:
            async with sm.begin() as db:
                market_id, meta, _ = await sync_market(gateway, db, strategy.market.coin)
            user = settings.hl_account_address

            factory: EngineFactory | None = None
            if settings.hl_api_wallet_private_key is not None:

                @asynccontextmanager
                async def engine_factory() -> AsyncIterator[ExecutionEngine]:
                    async with open_trading_session(settings, db_engine, gateway) as session:
                        yield await ExecutionEngine.create(
                            session, gateway, sm, strategy, notifier=notifier
                        )

                factory = engine_factory

            yield ApiContext(
                settings=settings,
                sm=sm,
                gateway=gateway,
                strategy=strategy,
                market_id=market_id,
                coin=meta.coin,
                user=user,
                risk=RiskEngine(
                    gateway,
                    sm,
                    strategy,
                    market_id=market_id,
                    coin=meta.coin,
                    user=user,
                    network=settings.network,
                    notifier=notifier,
                ),
                notifier=notifier,
                engine_factory=factory,
            )
    finally:
        await db_engine.dispose()
