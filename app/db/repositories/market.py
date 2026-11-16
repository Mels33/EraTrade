"""Запись и чтение рыночных данных. Функции не делают commit — транзакцией управляет вызывающий."""

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import func

from app.config.settings import Network
from app.db.models import CandleRecord, MarketContextSnapshotRecord, MarketRecord
from app.domain.market import AssetContext, Candle, MarketMeta


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
    if not candles:
        return 0
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
