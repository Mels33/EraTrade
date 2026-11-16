import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.execution.lock import (
    AnotherTraderRunningError,
    TraderLock,
    TraderLockLostError,
    lock_key,
)
from app.execution.nonce import MAX_NONCE_LEAD_MS, NonceError, NonceManager

SIGNER = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20


class FakeClockMs:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


async def test_nonces_unique_and_increasing_under_concurrency(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClockMs(1_000_000)  # часы стоят — уникальность обеспечивает +1
    manager = NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock)
    nonces = await asyncio.gather(*(manager.next() for _ in range(100)))
    assert len(set(nonces)) == 100
    assert sorted(nonces) == list(range(1_000_000, 1_000_100))


async def test_nonce_follows_clock_and_survives_restart(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClockMs(5_000)
    first = NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock)
    assert await first.next() == 5_000
    clock.now = 9_000
    assert await first.next() == 9_000  # опирается на время, когда оно впереди

    # «Рестарт» с отставшими часами: новый менеджер продолжает с last + 1.
    clock.now = 1_000
    restarted = NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock)
    assert await restarted.last() == 9_000
    assert await restarted.next() == 9_001


async def test_two_processes_same_signer_never_collide(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClockMs(42)
    a = NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock)
    b = NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock)
    results = await asyncio.gather(*(m.next() for m in [a, b] * 25))
    assert len(set(results)) == 50


async def test_signers_are_independent(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClockMs(100)
    assert await NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock).next() == 100
    assert await NonceManager(committing_sessionmaker, OTHER, clock_ms=clock).next() == 100


async def test_nonce_far_ahead_of_clock_is_refused(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    clock = FakeClockMs(10**12)
    manager = NonceManager(committing_sessionmaker, SIGNER, clock_ms=clock)
    await manager.next()
    clock.now -= MAX_NONCE_LEAD_MS + 60_000  # часы «ушли назад» больше чем на час
    with pytest.raises(NonceError, match="синхронизацию"):
        await manager.next()


async def test_only_one_trader_lock_per_signer(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        async with TraderLock(engine, SIGNER) as lock:
            await lock.ensure_held()
            with pytest.raises(AnotherTraderRunningError):
                async with TraderLock(engine, SIGNER.upper().replace("0X", "0x")):
                    pass
            async with TraderLock(engine, OTHER):  # другой API-кошелёк — свой процесс
                pass
        async with TraderLock(engine, SIGNER):  # после выхода блокировка свободна
            pass
    finally:
        await engine.dispose()


async def test_lock_released_when_connection_dies(migrated_db: str) -> None:
    engine = create_async_engine(migrated_db, poolclass=NullPool)
    try:
        lock = TraderLock(engine, SIGNER)
        await lock.__aenter__()
        assert lock._conn is not None
        await lock._conn.invalidate()  # как при падении процесса или обрыве сети
        with pytest.raises(TraderLockLostError):
            await lock.ensure_held()
        async with TraderLock(engine, SIGNER):  # PostgreSQL снял блокировку сам
            pass
    finally:
        await engine.dispose()


def test_lock_key_is_stable_and_case_insensitive() -> None:
    assert lock_key(SIGNER) == lock_key(SIGNER.upper().replace("0X", "0x"))
    assert lock_key(SIGNER) != lock_key(OTHER)
    assert -(2**63) <= lock_key(SIGNER) < 2**63
