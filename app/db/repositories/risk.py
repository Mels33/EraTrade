"""Агрегаты для risk engine. Без commit."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FillRecord, FundingEventRecord, OrderRecord
from app.domain.orders import OrderPurpose

QUOTE_ASSET = "USDC"  # комиссии в других токенах в лимит убытка не входят


async def realized_since(session: AsyncSession, since: datetime) -> tuple[Decimal, Decimal]:
    """(сумма closedPnl, сумма комиссий в USDC) по исполнениям с момента since."""
    stmt = select(
        func.coalesce(func.sum(FillRecord.closed_pnl), 0),
        func.coalesce(func.sum(FillRecord.fee).filter(FillRecord.fee_token == QUOTE_ASSET), 0),
    ).where(FillRecord.time >= since)
    pnl, fees = (await session.execute(stmt)).one()
    return Decimal(pnl), Decimal(fees)


async def funding_since(session: AsyncSession, since: datetime) -> Decimal:
    stmt = select(func.coalesce(func.sum(FundingEventRecord.usdc), 0)).where(
        FundingEventRecord.time >= since
    )
    return Decimal((await session.execute(stmt)).scalar_one())


async def entries_since(session: AsyncSession, market_id: int, since: datetime) -> int:
    """Число входов (с исполнением) с момента since."""
    stmt = select(func.count()).where(
        OrderRecord.market_id == market_id,
        OrderRecord.purpose == OrderPurpose.ENTRY.value,
        OrderRecord.filled_size > 0,
        OrderRecord.created_at >= since,
    )
    return int((await session.execute(stmt)).scalar_one())
