"""Зависимости API: БД и уведомления.

Ни ключа API-кошелька, ни запросов к бирже: панель читает состояние из БД (heartbeat и
исполнения маркет-мейкера) и меняет только режим бота; заявки ставит маркет-мейкер.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.config.strategy import StrategyConfig, load_strategy
from app.db.session import create_engine, create_sessionmaker
from app.observability.notify import Notifier, build_notifier


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class ApiContext:
    settings: Settings
    sm: async_sessionmaker[AsyncSession]
    strategy: StrategyConfig
    notifier: Notifier
    clock: Callable[[], datetime] = _utc_now


@asynccontextmanager
async def build_context(settings: Settings) -> AsyncIterator[ApiContext]:
    if settings.api_token is None:
        raise RuntimeError("не задан API_TOKEN — без него API не запускается")
    db_engine = create_engine(settings)
    try:
        yield ApiContext(
            settings=settings,
            sm=create_sessionmaker(db_engine),
            strategy=load_strategy(settings.strategy_config_path),
            notifier=build_notifier(settings),
        )
    finally:
        await db_engine.dispose()
