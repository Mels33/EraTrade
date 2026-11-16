"""Ордера, события ордеров, исполнения и снимки. Функции не делают commit."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccountSnapshotRecord,
    FillRecord,
    OrderEventRecord,
    OrderRecord,
    PositionSnapshotRecord,
)
from app.domain.account import AccountState, ActiveAssetData, Fill, Position
from app.domain.orders import OrderStatus, check_transition

UNRESOLVED = (
    OrderStatus.SIGNED,
    OrderStatus.UNKNOWN,
    OrderStatus.RESTING,
    OrderStatus.TRIGGERED,
    OrderStatus.PARTIALLY_FILLED,
)


async def create_order(session: AsyncSession, **fields: Any) -> OrderRecord:
    order = OrderRecord(status=OrderStatus.CREATED.value, **fields)
    session.add(order)
    await session.flush()
    session.add(
        OrderEventRecord(
            order_id=order.id,
            from_status=None,
            to_status=OrderStatus.CREATED.value,
            source="local",
            details={"intent": fields.get("intent", {})},
        )
    )
    await session.flush()
    return order


async def lock_order(session: AsyncSession, order_id: uuid.UUID) -> OrderRecord:
    stmt = select(OrderRecord).where(OrderRecord.id == order_id).with_for_update()
    return (await session.execute(stmt)).scalar_one()


async def transition(
    session: AsyncSession,
    order: OrderRecord,
    target: OrderStatus,
    *,
    source: str,
    details: dict[str, Any] | None = None,
    **updates: Any,
) -> OrderRecord:
    """Смена статуса с проверкой допустимости перехода и записью события."""
    current = OrderStatus(order.status)
    check_transition(current, target)
    for key, value in updates.items():
        setattr(order, key, value)
    order.status = target.value
    session.add(
        OrderEventRecord(
            order_id=order.id,
            from_status=current.value,
            to_status=target.value,
            source=source,
            details=details or {},
        )
    )
    await session.flush()
    return order


async def get_order(session: AsyncSession, order_id: uuid.UUID) -> OrderRecord | None:
    return await session.get(OrderRecord, order_id)


async def get_order_by_cloid(session: AsyncSession, cloid: str) -> OrderRecord | None:
    stmt = select(OrderRecord).where(OrderRecord.cloid == cloid)
    return (await session.execute(stmt)).scalar_one_or_none()


async def list_orders(
    session: AsyncSession,
    *,
    statuses: Sequence[OrderStatus] | None = None,
    market_id: int | None = None,
    limit: int = 50,
) -> list[OrderRecord]:
    stmt = select(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(limit)
    if statuses is not None:
        stmt = stmt.where(OrderRecord.status.in_([s.value for s in statuses]))
    if market_id is not None:
        stmt = stmt.where(OrderRecord.market_id == market_id)
    return list((await session.execute(stmt)).scalars())


async def list_events(session: AsyncSession, order_id: uuid.UUID) -> list[OrderEventRecord]:
    stmt = (
        select(OrderEventRecord)
        .where(OrderEventRecord.order_id == order_id)
        .order_by(OrderEventRecord.id)
    )
    return list((await session.execute(stmt)).scalars())


async def add_fills(session: AsyncSession, market_id: int, fills: Sequence[Fill]) -> int:
    """Идемпотентная запись исполнений (ключ — tid) с привязкой к нашим ордерам по oid/cloid.
    Возвращает число новых исполнений."""
    if not fills:
        return 0
    oids = {f.oid for f in fills}
    cloids = {f.cloid for f in fills if f.cloid}
    stmt = select(OrderRecord.id, OrderRecord.oid, OrderRecord.cloid).where(
        (OrderRecord.oid.in_(oids)) | (OrderRecord.cloid.in_(cloids))
    )
    by_oid: dict[int, uuid.UUID] = {}
    by_cloid: dict[str, uuid.UUID] = {}
    for order_id, oid, cloid in (await session.execute(stmt)).all():
        if oid is not None:
            by_oid[oid] = order_id
        by_cloid[cloid] = order_id
    rows = [
        {
            "tid": f.tid,
            "order_id": by_oid.get(f.oid) or (by_cloid.get(f.cloid) if f.cloid else None),
            "oid": f.oid,
            "cloid": f.cloid,
            "market_id": market_id,
            "coin": f.coin,
            "side": f.side.value,
            "price": f.price,
            "size": f.size,
            "fee": f.fee,
            "fee_token": f.fee_token,
            "closed_pnl": f.closed_pnl,
            "direction": f.direction,
            "start_position": f.start_position,
            "crossed": f.crossed,
            "hash": f.hash,
            "time": f.time,
            "raw": f.raw,
        }
        for f in fills
    ]
    insert_stmt = insert(FillRecord).values(rows)
    stmt_ins = insert_stmt.on_conflict_do_nothing(index_elements=["tid"]).returning(FillRecord.tid)
    return len((await session.execute(stmt_ins)).all())


async def fill_totals(session: AsyncSession, order_id: uuid.UUID) -> tuple[Decimal, Decimal | None]:
    """(исполненный размер, средняя цена) по сохранённым fills ордера."""
    stmt = select(func.sum(FillRecord.size), func.sum(FillRecord.size * FillRecord.price)).where(
        FillRecord.order_id == order_id
    )
    size, notional = (await session.execute(stmt)).one()
    if not size:
        return Decimal(0), None
    return Decimal(size), Decimal(notional) / Decimal(size)


async def add_position_snapshot(
    session: AsyncSession, market_id: int, position: Position | None, exchange_time: datetime
) -> None:
    session.add(
        PositionSnapshotRecord(
            market_id=market_id,
            size=position.size if position else Decimal(0),
            entry_price=position.entry_price if position else None,
            unrealized_pnl=position.unrealized_pnl if position else Decimal(0),
            liquidation_price=position.liquidation_price if position else None,
            margin_used=position.margin_used if position else Decimal(0),
            leverage_type=position.leverage_type if position else None,
            leverage=position.leverage if position else None,
            exchange_time=exchange_time,
            raw=position.raw if position else None,
        )
    )
    await session.flush()


async def add_account_snapshot(
    session: AsyncSession, state: AccountState, asset: ActiveAssetData | None
) -> None:
    session.add(
        AccountSnapshotRecord(
            account_value=state.account_value,
            total_margin_used=state.total_margin_used,
            withdrawable=state.withdrawable,
            available_to_trade=asset.available_to_trade_buy if asset else None,
            exchange_time=state.exchange_time,
            raw={"marginSummary": state.raw.get("marginSummary")},
        )
    )
    await session.flush()
