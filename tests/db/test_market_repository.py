from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Network
from app.db.models import CandleRecord
from app.db.repositories import market as repo
from app.domain.market import DataSource
from tests.factories import HOUR, T0, asset_context, candle, market_meta


async def _count_candles(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(CandleRecord))).scalar_one()


async def test_upsert_market_is_idempotent_and_updates(session: AsyncSession) -> None:
    first = await repo.upsert_market(session, market_meta())
    second = await repo.upsert_market(session, market_meta(sz_decimals=4, asset_id=7))
    assert first == second

    record = await repo.get_market(session, Network.TESTNET, "BTC")
    assert record is not None
    assert record.sz_decimals == 4
    assert record.asset_id == 7


async def test_markets_separated_by_network(session: AsyncSession) -> None:
    testnet_id = await repo.upsert_market(session, market_meta())
    mainnet_id = await repo.upsert_market(session, market_meta(network=Network.MAINNET))
    assert testnet_id != mainnet_id
    assert await repo.get_market(session, Network.MAINNET, "ETH") is None


async def test_candle_upsert_lifecycle(session: AsyncSession) -> None:
    market_id = await repo.upsert_market(session, market_meta())

    # Незакрытая свеча из WS вставляется и обновляется.
    live = candle(0, is_closed=False, source=DataSource.WS, close=Decimal("90100"))
    assert await repo.upsert_candles(session, market_id, [live]) == 1
    updated = candle(0, is_closed=False, source=DataSource.WS, close=Decimal("90150"))
    assert await repo.upsert_candles(session, market_id, [updated]) == 1

    # Закрытие свечи.
    closed = candle(0, is_closed=True, source=DataSource.WS, close=Decimal("90200"))
    assert await repo.upsert_candles(session, market_id, [closed]) == 1

    # Повтор того же события и попытка переписать закрытую свечу ничего не меняют.
    assert await repo.upsert_candles(session, market_id, [closed]) == 0
    late = candle(0, is_closed=False, source=DataSource.REST, close=Decimal("89900"))
    assert await repo.upsert_candles(session, market_id, [late]) == 0

    rows = await repo.get_candles(session, market_id, "1h")
    assert len(rows) == 1
    assert rows[0].close == Decimal("90200")
    assert rows[0].source == "ws"
    assert await _count_candles(session) == 1


async def test_batch_upsert_and_queries(session: AsyncSession) -> None:
    market_id = await repo.upsert_market(session, market_meta())
    batch = [candle(i) for i in range(5)] + [candle(5, is_closed=False)]
    assert await repo.upsert_candles(session, market_id, batch) == 6
    assert await repo.upsert_candles(session, market_id, batch) == 1  # обновилась только открытая

    closed = await repo.get_candles(session, market_id, "1h")
    assert [r.open_time for r in closed] == [T0 + i * HOUR for i in range(5)]

    everything = await repo.get_candles(session, market_id, "1h", closed_only=False)
    assert len(everything) == 6

    window = await repo.get_candles(session, market_id, "1h", start=T0 + HOUR, end=T0 + 3 * HOUR)
    assert [r.open_time for r in window] == [T0 + HOUR, T0 + 2 * HOUR]

    last = await repo.get_last_closed_candle(session, market_id, "1h")
    assert last is not None and last.open_time == T0 + 4 * HOUR

    assert await repo.upsert_candles(session, market_id, []) == 0


async def test_decimal_precision_and_timezone_preserved(session: AsyncSession) -> None:
    market_id = await repo.upsert_market(session, market_meta())
    precise = Decimal("0.000123456789012345678901")
    await repo.upsert_candles(
        session,
        market_id,
        [candle(0, open=precise, low=precise, close=precise, high=Decimal("1"), volume=precise)],
    )
    [row] = await repo.get_candles(session, market_id, "1h")
    assert row.open == precise
    assert row.volume == precise
    assert row.open_time == T0
    assert row.open_time.utcoffset() == timedelta(0)


async def test_db_rejects_inconsistent_candle(session: AsyncSession) -> None:
    market_id = await repo.upsert_market(session, market_meta())
    bad = CandleRecord(
        market_id=market_id,
        interval="1h",
        open_time=T0,
        close_time=T0 + HOUR,
        open=Decimal(1),
        high=Decimal(1),
        low=Decimal(2),  # low > high
        close=Decimal(1),
        volume=Decimal(0),
        trades_count=0,
        is_closed=True,
        source="rest",
        received_at=T0,
    )
    with pytest.raises(IntegrityError, match="ck_candles_high_low"):
        async with session.begin_nested():
            session.add(bad)
            await session.flush()


async def test_context_snapshots(session: AsyncSession) -> None:
    market_id = await repo.upsert_market(session, market_meta())
    assert await repo.get_latest_context(session, market_id) is None
    await repo.add_context_snapshot(session, market_id, asset_context())
    await repo.add_context_snapshot(
        session, market_id, asset_context(received_at=T0 + HOUR, funding=Decimal("-0.00001"))
    )
    latest = await repo.get_latest_context(session, market_id)
    assert latest is not None
    assert latest.funding == Decimal("-0.00001")
    assert latest.exchange_time is None
