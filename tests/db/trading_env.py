"""Общее окружение торговых тестов: симулятор Hyperliquid + сессия + движок + PostgreSQL."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Network, Settings, TradingMode
from app.config.strategy import load_strategy
from app.db.models import OrderRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.db.repositories import trading as trading_repo
from app.domain.control import Actor, ActorKind
from app.domain.market import Candle, DataSource
from app.execution.engine import ExecutionEngine, OrderPreview
from app.execution.session import open_trading_session
from app.integrations.hyperliquid.exchange_client import ExchangeClient
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.info_client import InfoClient
from app.integrations.hyperliquid.parsing import datetime_to_ms
from app.reconciliation.reconciler import Reconciler
from tests.db.constants import TEST_DB_URL
from tests.execution.fake_hyperliquid import FakeHyperliquid

ROOT = Path(__file__).resolve().parents[2]
TEST_KEY = "0x" + "33" * 32
SIGNER = Account.from_key(TEST_KEY).address.lower()
ACCOUNT = "0x" + "c4" * 20
NOW = datetime(2026, 11, 17, 12, 0, 30, tzinfo=UTC)
OPERATOR = Actor(ActorKind.OPERATOR, "tester")
BASE = "https://example.invalid"


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class RecordingNotifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send(self, text: str) -> bool:
        self.messages.append(text)
        return True


@dataclass
class Env:
    fake: FakeHyperliquid
    engine: ExecutionEngine
    sm: async_sessionmaker[AsyncSession]
    clock: Clock
    previews: list[OrderPreview]
    notifier: RecordingNotifier

    async def yes(self, preview: OrderPreview) -> bool:
        self.previews.append(preview)
        return True

    async def no(self, preview: OrderPreview) -> bool:
        self.previews.append(preview)
        return False

    async def order(self, order_id: Any) -> OrderRecord:
        async with self.sm() as db:
            order = await trading_repo.get_order(db, order_id)
            assert order is not None
            return order

    async def statuses(self, order_id: Any) -> list[str]:
        async with self.sm() as db:
            return [e.to_status for e in await trading_repo.list_events(db, order_id)]

    async def set_mode(self, mode: TradingMode) -> None:
        async with self.sm.begin() as db:
            await control.set_trading_mode(db, Network.TESTNET, mode, actor=OPERATOR, reason="test")

    def order_actions(self) -> list[dict[str, Any]]:
        return [a for a in self.fake.exchange_actions if a["type"] == "order"]

    def reconciler(self) -> Reconciler:
        return Reconciler(
            self.engine.gateway,
            self.sm,
            self.engine.tracker,
            network=Network.TESTNET,
            market_id=self.engine.market_id,
            coin=self.engine.coin,
            user=ACCOUNT,
            clock=self.clock,
            risk=self.engine.risk,
            notifier=self.notifier,
        )


async def seed_candles(sm: async_sessionmaker[AsyncSession], market_id: int) -> None:
    candles = [
        Candle(
            interval="1h",
            open_time=NOW.replace(minute=0, second=0) - timedelta(hours=20 - i),
            close_time=NOW.replace(minute=0, second=0) - timedelta(hours=19 - i),
            open=Decimal("65000"),
            high=Decimal("65100"),
            low=Decimal("64900"),
            close=Decimal("65000"),
            volume=Decimal("100"),
            trades_count=1000,
            is_closed=True,
            source=DataSource.REST,
            received_at=NOW,
        )
        for i in range(20)
    ]  # TR = 200 → ATR(14) = 200 → дистанция стопа max(300, 0.5% × 65000 = 325) = 325
    async with sm.begin() as db:
        await market_repo.upsert_candles(db, market_id, candles)


@pytest.fixture
async def env(committing_sessionmaker: async_sessionmaker[AsyncSession]) -> AsyncIterator[Env]:
    sm = committing_sessionmaker
    clock = Clock(NOW)
    fake = FakeHyperliquid(signer=SIGNER, account=ACCOUNT, now_ms=datetime_to_ms(NOW))
    transport = httpx.MockTransport(fake)
    gateway = HyperliquidGateway(
        InfoClient(BASE, transport=transport), Network.TESTNET, clock=clock
    )
    settings = Settings(
        _env_file=None,
        environment="testnet",
        hyperliquid_network="testnet",
        database_url=TEST_DB_URL,
        hl_account_address=ACCOUNT,
        hl_api_wallet_private_key=TEST_KEY,
    )
    strategy = load_strategy(ROOT / "config" / "strategy.toml")
    db_engine = create_async_engine(TEST_DB_URL, poolclass=NullPool)
    async with sm.begin() as db:
        await control.set_trading_mode(
            db, Network.TESTNET, TradingMode.TESTNET, actor=OPERATOR, reason="test"
        )
    try:
        async with open_trading_session(
            settings,
            db_engine,
            gateway,
            exchange=ExchangeClient(BASE, transport=transport),
            clock=clock,
        ) as session:
            notifier = RecordingNotifier()
            engine = await ExecutionEngine.create(
                session, gateway, sm, strategy, notifier=notifier, clock=clock
            )
            await seed_candles(sm, engine.market_id)
            yield Env(fake, engine, sm, clock, [], notifier)
    finally:
        await db_engine.dispose()
