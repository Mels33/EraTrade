"""Сверка с биржей (этап 7, §6.5 и §11 плана)."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, CheckConstraint, ForeignKey, Identity, Index, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReconciliationRunRecord(Base):
    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        CheckConstraint(
            "status in ('running', 'ok', 'warnings', 'critical', 'error')", name="status"
        ),
        Index("ix_reconciliation_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    trigger: Mapped[str]  # startup, periodic, pre_trade, manual
    status: Mapped[str]
    started_at: Mapped[datetime] = mapped_column(server_default=func.now())
    finished_at: Mapped[datetime | None]
    summary: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class ReconciliationDifferenceRecord(Base):
    __tablename__ = "reconciliation_differences"
    __table_args__ = (
        CheckConstraint("severity in ('info', 'warning', 'critical')", name="severity"),
        Index("ix_reconciliation_differences_run_id", "run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_runs.id"))
    kind: Mapped[str]
    severity: Mapped[str]
    repaired: Mapped[bool]  # расхождение устранено автоматически в этом прогоне
    message: Mapped[str]
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FundingEventRecord(Base):
    """Фактические платежи funding (ежечасно для perp)."""

    __tablename__ = "funding_events"
    __table_args__ = (Index("ix_funding_events_time", "time"),)

    hash: Mapped[str] = mapped_column(primary_key=True)
    coin: Mapped[str] = mapped_column(primary_key=True)
    time: Mapped[datetime]
    usdc: Mapped[Decimal]  # > 0 получено, < 0 уплачено
    position_size: Mapped[Decimal]
    funding_rate: Mapped[Decimal]
    raw: Mapped[dict[str, Any]]


class LedgerEventRecord(Base):
    """Движения по счёту, кроме funding: депозиты, выводы, переводы."""

    __tablename__ = "ledger_events"
    __table_args__ = (Index("ix_ledger_events_time", "time"),)

    hash: Mapped[str] = mapped_column(primary_key=True)
    delta_type: Mapped[str] = mapped_column(primary_key=True)
    time: Mapped[datetime]
    usdc: Mapped[Decimal | None]
    raw: Mapped[dict[str, Any]]


class PositionBaselineRecord(Base):
    """Подтверждённая оператором позиция — точка отсчёта для ожидаемой позиции бота.

    Ожидаемая позиция = последний baseline + исполнения ордеров бота после него.
    Нужна, когда позиция на бирже изменилась вне бота и оператор признал это состояние.
    """

    __tablename__ = "position_baselines"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    size: Mapped[Decimal]
    effective_at: Mapped[datetime]
    actor: Mapped[str]
    reason: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
