"""Запись и чтение рыночных данных. Функции не делают commit — транзакцией управляет вызывающий."""

from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.config.settings import Network
from app.db.models import (
    CandleRecord,
    FundingHistoryRecord,
    MarketContextSnapshotRecord,
    MarketRecord,
)
from app.domain.market import AssetContext, Candle, FundingPoint, MarketMeta

# 14 параметров на свечу; у PostgreSQL предел 32767 параметров на запрос.
CANDLE_BATCH_SIZE = 1000
FUNDING_BATCH_SIZE = 5000


async def upsert_market(session: AsyncSession, meta: MarketMeta) -> int:
    """Создаёт или обновляет метаданные рынка, возвращает markets.id."""
    values = {
        "network": meta.network.value,
        "dex": meta.dex,
        "coin": meta.coin,
        "asset_id": meta.asset_id,
        "sz_decimals": meta.sz_decimals,
        "max_leverage": meta.max_leverage,
        "only_isolated": meta.only_isolated,
        "is_delisted": meta.is_delisted,
        "margin_table_id": meta.margin_table_id,
        "raw_meta": meta.raw,
        "meta_updated_at": meta.updated_at,
    }
    insert_stmt = insert(MarketRecord).values(**values)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["network", "dex", "coin"],
        set_={k: insert_stmt.excluded[k] for k in values if k not in ("network", "dex", "coin")},
    ).returning(MarketRecord.id)
    market_id: int = (await session.execute(stmt)).scalar_one()
    return market_id


async def get_market(
    session: AsyncSession, network: Network, coin: str, dex: str = ""
) -> MarketRecord | None:
    stmt = select(MarketRecord).where(
        MarketRecord.network == network.value,
        MarketRecord.dex == dex,
        MarketRecord.coin == coin,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def upsert_candles(session: AsyncSession, market_id: int, candles: Sequence[Candle]) -> int:
    """Идемпотентная запись свечей.

    - новая свеча вставляется;
    - незакрытая свеча обновляется (в т. ч. при закрытии);
    - закрытая свеча больше не меняется — повторная обработка того же события ничего не делает.

    Возвращает число реально вставленных или изменённых строк.
    """
    written = 0
    for offset in range(0, len(candles), CANDLE_BATCH_SIZE):
        written += await _upsert_candle_batch(
            session, market_id, candles[offset : offset + CANDLE_BATCH_SIZE]
        )
    return written


async def _upsert_candle_batch(
    session: AsyncSession, market_id: int, candles: Sequence[Candle]
) -> int:
    rows = [
        {
            "market_id": market_id,
            "interval": c.interval,
            "open_time": c.open_time,
            "close_time": c.close_time,
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "trades_count": c.trades_count,
            "is_closed": c.is_closed,
            "source": c.source.value,
            "received_at": c.received_at,
            "raw": c.raw,
        }
        for c in candles
    ]
    updatable = (
        "close_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "trades_count",
        "is_closed",
        "source",
        "received_at",
        "raw",
    )
    insert_stmt = insert(CandleRecord).values(rows)
    stmt = insert_stmt.on_conflict_do_update(
        index_elements=["market_id", "interval", "open_time"],
        set_={**{k: insert_stmt.excluded[k] for k in updatable}, "updated_at": func.now()},
        where=CandleRecord.is_closed.is_(False),
    ).returning(CandleRecord.open_time)
    result = await session.execute(stmt)
    return len(result.all())


async def get_candles(
    session: AsyncSession,
    market_id: int,
    interval: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    closed_only: bool = True,
) -> list[CandleRecord]:
    """Свечи по возрастанию open_time в полуинтервале [start, end)."""
    stmt = select(CandleRecord).where(
        CandleRecord.market_id == market_id, CandleRecord.interval == interval
    )
    if closed_only:
        stmt = stmt.where(CandleRecord.is_closed.is_(True))
    if start is not None:
        stmt = stmt.where(CandleRecord.open_time >= start)
    if end is not None:
        stmt = stmt.where(CandleRecord.open_time < end)
    stmt = stmt.order_by(CandleRecord.open_time)
    return list((await session.execute(stmt)).scalars())


async def get_last_closed_candle(
    session: AsyncSession, market_id: int, interval: str
) -> CandleRecord | None:
    stmt = (
        select(CandleRecord)
        .where(
            CandleRecord.market_id == market_id,
            CandleRecord.interval == interval,
            CandleRecord.is_closed.is_(True),
        )
        .order_by(CandleRecord.open_time.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_context_snapshot(session: AsyncSession, market_id: int, ctx: AssetContext) -> int:
    record = MarketContextSnapshotRecord(
        market_id=market_id,
        exchange_time=ctx.exchange_time,
        received_at=ctx.received_at,
        mark_price=ctx.mark_price,
        oracle_price=ctx.oracle_price,
        mid_price=ctx.mid_price,
        funding=ctx.funding,
        open_interest=ctx.open_interest,
        premium=ctx.premium,
        day_notional_volume=ctx.day_notional_volume,
        impact_bid_price=ctx.impact_bid_price,
        impact_ask_price=ctx.impact_ask_price,
        source=ctx.source.value,
        raw=ctx.raw,
    )
    session.add(record)
    await session.flush()
    return record.id


async def get_latest_context(
    session: AsyncSession, market_id: int
) -> MarketContextSnapshotRecord | None:
    stmt = (
        select(MarketContextSnapshotRecord)
        .where(MarketContextSnapshotRecord.market_id == market_id)
        .order_by(MarketContextSnapshotRecord.received_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def find_missing_candles(
    session: AsyncSession,
    market_id: int,
    interval: str,
    step: timedelta,
    start: datetime,
    end: datetime,
) -> list[datetime]:
    """open_time закрытых свечей, которых нет в [start, end) с шагом step."""
    stmt = text(
        """
        SELECT gs AS open_time
        FROM generate_series(CAST(:start AS timestamptz), CAST(:last AS timestamptz),
                             CAST(:step AS interval)) AS gs
        LEFT JOIN candles c
               ON c.market_id = :market_id AND c.interval = :interval
              AND c.open_time = gs AND c.is_closed
        WHERE c.open_time IS NULL
        ORDER BY gs
        """
    )
    if end <= start:
        return []
    params = {
        "start": start,
        "last": end - step,
        "step": step,
        "market_id": market_id,
        "interval": interval,
    }
    return list((await session.execute(stmt, params)).scalars())


async def candle_stats(
    session: AsyncSession, market_id: int, interval: str
) -> tuple[int, datetime | None, datetime | None]:
    """(число закрытых свечей, первая open_time, последняя open_time)."""
    stmt = select(
        func.count(), func.min(CandleRecord.open_time), func.max(CandleRecord.open_time)
    ).where(
        CandleRecord.market_id == market_id,
        CandleRecord.interval == interval,
        CandleRecord.is_closed.is_(True),
    )
    count, first, last = (await session.execute(stmt)).one()
    return int(count), first, last


# --- funding рынка ---


async def upsert_funding_history(
    session: AsyncSession, market_id: int, points: Sequence[FundingPoint]
) -> int:
    """Идемпотентная запись почасового funding. Возвращает число новых/изменённых строк."""
    written = 0
    for i in range(0, len(points), FUNDING_BATCH_SIZE):
        rows = [
            {
                "market_id": market_id,
                "time": p.time,
                "funding_rate": p.funding_rate,
                "premium": p.premium,
            }
            for p in points[i : i + FUNDING_BATCH_SIZE]
        ]
        insert_stmt = insert(FundingHistoryRecord).values(rows)
        excluded = insert_stmt.excluded
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["market_id", "time"],
            set_={"funding_rate": excluded.funding_rate, "premium": excluded.premium},
            where=(FundingHistoryRecord.funding_rate != excluded.funding_rate)
            | (FundingHistoryRecord.premium != excluded.premium),
        ).returning(FundingHistoryRecord.time)
        written += len((await session.execute(stmt)).all())
    return written


async def get_funding_history(
    session: AsyncSession,
    market_id: int,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[FundingPoint]:
    """Funding по возрастанию времени в полуинтервале [start, end)."""
    stmt = select(FundingHistoryRecord).where(FundingHistoryRecord.market_id == market_id)
    if start is not None:
        stmt = stmt.where(FundingHistoryRecord.time >= start)
    if end is not None:
        stmt = stmt.where(FundingHistoryRecord.time < end)
    stmt = stmt.order_by(FundingHistoryRecord.time)
    return [
        FundingPoint(time=r.time, funding_rate=r.funding_rate, premium=r.premium)
        for r in (await session.execute(stmt)).scalars()
    ]


async def get_last_funding_time(session: AsyncSession, market_id: int) -> datetime | None:
    stmt = select(func.max(FundingHistoryRecord.time)).where(
        FundingHistoryRecord.market_id == market_id
    )
    return (await session.execute(stmt)).scalar_one_or_none()
