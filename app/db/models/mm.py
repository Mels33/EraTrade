"""Исполнения маркет-мейкера (монеты из [mm], отдельно от основной стратегии)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Index, false, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class MmFillRecord(Base):
    __tablename__ = "mm_fills"
    __table_args__ = (Index("ix_mm_fills_time", "time"),)

    tid: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    oid: Mapped[int] = mapped_column(BigInteger)
    cloid: Mapped[str | None]
    coin: Mapped[str]
    side: Mapped[str]
    price: Mapped[Decimal]
    size: Mapped[Decimal]
    fee: Mapped[Decimal]
    closed_pnl: Mapped[Decimal]
    direction: Mapped[str]
    crossed: Mapped[bool] = mapped_column(server_default=false())
    time: Mapped[datetime]
    raw: Mapped[dict[str, Any]]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
