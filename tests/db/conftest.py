"""Интеграционные тесты на настоящем PostgreSQL.

Используется отдельная БД (по умолчанию eratrade_pytest_testnet). Она пересоздаётся
в начале сессии, миграции прогоняются вверх-вниз-вверх. Каждый тест работает в
транзакции, которая откатывается. Если PostgreSQL недоступен — тесты пропускаются.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import asyncpg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import get_settings
from tests.db.constants import TEST_DB_URL

ROOT = Path(__file__).resolve().parents[2]


async def _recreate_database(url: str) -> None:
    parsed = make_url(url)
    name = parsed.database or ""
    # Защита: эта фикстура удаляет БД, поэтому работает только с тестовой.
    assert "pytest" in name, f"тестовая БД должна содержать 'pytest' в имени: {name}"
    conn = await asyncpg.connect(
        user=parsed.username,
        password=parsed.password,
        host=parsed.host,
        port=parsed.port,
        database="postgres",
        timeout=3,
    )
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[str]:
    try:
        asyncio.run(_recreate_database(TEST_DB_URL))
    except (OSError, TimeoutError, asyncpg.PostgresError) as exc:
        pytest.skip(f"PostgreSQL недоступен: {exc}")

    mp = pytest.MonkeyPatch()
    mp.setenv("ERATRADE_ENV_FILE", str(ROOT / "tests" / "nonexistent.env"))
    mp.setenv("ENVIRONMENT", "testnet")
    mp.setenv("HYPERLIQUID_NETWORK", "testnet")
    mp.setenv("TRADING_MODE", "DISABLED")
    mp.setenv("DATABASE_URL", TEST_DB_URL)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ROOT / "alembic.ini"))
        command.upgrade(cfg, "head")
        command.downgrade(cfg, "base")  # миграции обратимы
        command.upgrade(cfg, "head")
    finally:
        mp.undo()
        get_settings.cache_clear()
    yield TEST_DB_URL


@pytest.fixture
async def session(migrated_db: str) -> AsyncIterator[AsyncSession]:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    async with engine.connect() as conn:
        trans = await conn.begin()
        s = AsyncSession(
            bind=conn, expire_on_commit=False, join_transaction_mode="create_savepoint"
        )
        try:
            yield s
        finally:
            await s.close()
            await trans.rollback()
    await engine.dispose()


@pytest.fixture
async def committing_sessionmaker(
    migrated_db: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Для кода, который сам управляет транзакциями и работает из нескольких задач.
    Данные реально коммитятся; рыночные таблицы очищаются до и после теста
    (audit_log не трогается — его очистка запрещена триггером)."""
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    truncate = text(
        "TRUNCATE candles, market_context_snapshots, heartbeats, markets, nonce_state, "
        "bot_state, account_snapshots, funding_events, ledger_events CASCADE"
    )
    async with engine.begin() as conn:
        await conn.execute(truncate)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as conn:
            await conn.execute(truncate)
        await engine.dispose()


# Общее окружение торговых тестов (симулятор биржи + движок): регистрируем фикстуру `env`.
from tests.db.trading_env import env  # noqa: E402, F401
