"""Торговые таблицы (этап 6, §6.4 плана).

orders — каждое намерение сохраняется ДО обращения к бирже (статус CREATED); строка в
статусах CREATED/APPROVED — это и есть order intent из плана. Все изменения статуса пишутся
в order_events. fills дедуплицируются по tid биржи.
"""

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    Uuid,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.domain.orders import OrderKind, OrderPurpose, OrderStatus

_STATUSES = ", ".join(f"'{s.value}'" for s in OrderStatus)
_PURPOSES = ", ".join(f"'{p.value}'" for p in OrderPurpose)
_KINDS = ", ".join(f"'{k.value}'" for k in OrderKind)


class OrderRecord(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(f"status in ({_STATUSES})", name="status"),
        CheckConstraint(f"purpose in ({_PURPOSES})", name="purpose"),
        CheckConstraint(f"order_kind in ({_KINDS})", name="order_kind"),
        CheckConstraint("side in ('buy', 'sell')", name="side"),
        CheckConstraint("size > 0 and price > 0", name="positive"),
        CheckConstraint("filled_size >= 0 and filled_size <= size", name="filled_size"),
        CheckConstraint("purpose = 'entry' or reduce_only", name="exit_reduce_only"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_oid", "oid"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cloid: Mapped[str] = mapped_column(unique=True)
    oid: Mapped[int | None] = mapped_column(BigInteger)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    coin: Mapped[str]
    purpose: Mapped[str]
    order_kind: Mapped[str]
    side: Mapped[str]
    price: Mapped[Decimal]
    size: Mapped[Decimal]
    trigger_price: Mapped[Decimal | None]
    reduce_only: Mapped[bool]
    status: Mapped[str]
    filled_size: Mapped[Decimal] = mapped_column(server_default=text("0"))
    avg_fill_price: Mapped[Decimal | None]
    nonce: Mapped[int | None] = mapped_column(BigInteger)
    vault_address: Mapped[str | None]
    parent_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    correlation_id: Mapped[str | None]
    intent: Mapped[dict[str, Any]]  # запрошенные значения, нормализация, проверки guard
    request_action: Mapped[dict[str, Any] | None]  # подписанное действие (без подписи)
    response: Mapped[dict[str, Any] | None]  # сырой ответ биржи
    error: Mapped[str | None]
    error_code: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    signed_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class OrderEventRecord(Base):
    __tablename__ = "order_events"
    __table_args__ = (Index("ix_order_events_order_id", "order_id", "id"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    order_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("orders.id"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    from_status: Mapped[str | None]
    to_status: Mapped[str]
    source: Mapped[str]  # local, exchange_response, order_status, fills, operator
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class FillRecord(Base):
    __tablename__ = "fills"
    __table_args__ = (Index("ix_fills_oid", "oid"), Index("ix_fills_time", "time"))

    tid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    oid: Mapped[int] = mapped_column(BigInteger)
    cloid: Mapped[str | None]
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    coin: Mapped[str]
    side: Mapped[str]
    price: Mapped[Decimal]
    size: Mapped[Decimal]
    fee: Mapped[Decimal]
    fee_token: Mapped[str]
    closed_pnl: Mapped[Decimal]
    direction: Mapped[str]
    start_position: Mapped[Decimal]
    crossed: Mapped[bool] = mapped_column(server_default=false())
    hash: Mapped[str]
    time: Mapped[datetime]
    raw: Mapped[dict[str, Any]]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PositionSnapshotRecord(Base):
    __tablename__ = "position_snapshots"
    __table_args__ = (Index("ix_position_snapshots_market_time", "market_id", "received_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    size: Mapped[Decimal]
    entry_price: Mapped[Decimal | None]
    unrealized_pnl: Mapped[Decimal]
    liquidation_price: Mapped[Decimal | None]
    margin_used: Mapped[Decimal]
    leverage_type: Mapped[str | None]
    leverage: Mapped[int | None]
    exchange_time: Mapped[datetime]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    raw: Mapped[dict[str, Any] | None]


class AccountSnapshotRecord(Base):
    __tablename__ = "account_snapshots"
    __table_args__ = (Index("ix_account_snapshots_received_at", "received_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    account_value: Mapped[Decimal]
    total_margin_used: Mapped[Decimal]
    withdrawable: Mapped[Decimal]
    available_to_trade: Mapped[Decimal | None]  # с учётом spot USDC (activeAssetData)
    exchange_time: Mapped[datetime]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
    raw: Mapped[dict[str, Any] | None]
