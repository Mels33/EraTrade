"""Исполнения маркет-мейкера: запись без дублей и сводки PnL."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Date, cast, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import MmFillRecord
from app.domain.account import Fill


async def add_fills(session: AsyncSession, fills: Sequence[Fill]) -> set[int]:
    """Новые исполнения (дубли по tid пропускаются). Возвращает tid добавленных."""
    if not fills:
        return set()
    rows = [
        {
            "tid": f.tid,
            "oid": f.oid,
            "cloid": f.cloid,
            "coin": f.coin,
            "side": f.side.value,
            "price": f.price,
            "size": f.size,
            "fee": f.fee,
            "closed_pnl": f.closed_pnl,
            "direction": f.direction,
            "crossed": f.crossed,
            "time": f.time,
            "raw": f.raw,
        }
        for f in fills
    ]
    stmt = insert(MmFillRecord).values(rows).on_conflict_do_nothing(index_elements=["tid"])
    result = await session.execute(stmt.returning(MmFillRecord.tid))
    return set(result.scalars())


async def realized_since(session: AsyncSession, since: datetime) -> Decimal:
    """Реализованный PnL после комиссий: Σ(closedPnl − fee)."""
    stmt = select(func.coalesce(func.sum(MmFillRecord.closed_pnl - MmFillRecord.fee), 0)).where(
        MmFillRecord.time >= since
    )
    return Decimal((await session.execute(stmt)).scalar_one())


async def last_fill_time(session: AsyncSession) -> datetime | None:
    stmt = select(func.max(MmFillRecord.time))
    return (await session.execute(stmt)).scalar_one()


async def recent_fills(session: AsyncSession, limit: int = 50) -> list[MmFillRecord]:
    stmt = select(MmFillRecord).order_by(MmFillRecord.time.desc(), MmFillRecord.tid.desc())
    return list((await session.execute(stmt.limit(limit))).scalars())


async def daily_summary(session: AsyncSession, since: datetime) -> list[dict[str, Any]]:
    """По дням UTC и монетам: число исполнений, оборот, комиссии, чистый результат."""
    day = cast(func.timezone("UTC", MmFillRecord.time), Date)
    stmt = (
        select(
            day.label("day"),
            MmFillRecord.coin,
            func.count().label("fills"),
            func.sum(MmFillRecord.price * MmFillRecord.size).label("volume"),
            func.sum(MmFillRecord.fee).label("fees"),
            func.sum(MmFillRecord.closed_pnl - MmFillRecord.fee).label("net"),
            func.count().filter(MmFillRecord.crossed).label("taker_fills"),
        )
        .where(MmFillRecord.time >= since)
        .group_by(day, MmFillRecord.coin)
        .order_by(day.desc(), MmFillRecord.coin)
    )
    return [
        {
            "day": r.day.isoformat(),
            "coin": r.coin,
            "fills": r.fills,
            "volume": r.volume,
            "fees": r.fees,
            "net": r.net,
            "taker_fills": r.taker_fills,
        }
        for r in (await session.execute(stmt)).all()
    ]


async def pnl_by_minute(session: AsyncSession, since: datetime) -> list[tuple[datetime, Decimal]]:
    """Чистый результат исполнений по минутам (для графика накопленного PnL)."""
    minute = func.date_trunc("minute", MmFillRecord.time)
    stmt = (
        select(minute, func.sum(MmFillRecord.closed_pnl - MmFillRecord.fee))
        .where(MmFillRecord.time >= since)
        .group_by(minute)
        .order_by(minute)
    )
    return [(t, Decimal(v)) for t, v in (await session.execute(stmt)).all()]


async def last_buy_price(session: AsyncSession, coin: str) -> Decimal | None:
    """Цена последней покупки по монете (восстановление уровня после перезапуска)."""
    stmt = (
        select(MmFillRecord.price)
        .where(MmFillRecord.coin == coin, MmFillRecord.side == "buy")
        .order_by(MmFillRecord.time.desc(), MmFillRecord.tid.desc())
        .limit(1)
    )
    value = (await session.execute(stmt)).scalar_one_or_none()
    return Decimal(value) if value is not None else None
