import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

MODEL_STATUS_CHECK = "status in ('candidate', 'shadow', 'active', 'retired')"


class ModelVersionRecord(Base):
    """Версии моделей (§15.4): не больше одной active и одной shadow."""

    __tablename__ = "model_versions"
    __table_args__ = (
        CheckConstraint(MODEL_STATUS_CHECK, name="status"),
        Index(
            "uq_model_versions_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
        ),
        Index(
            "uq_model_versions_one_shadow",
            "status",
            unique=True,
            postgresql_where=text("status = 'shadow'"),
        ),
    )

    version: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str]
    feature_set_version: Mapped[str]
    model_sha256: Mapped[str]
    artifact_path: Mapped[str]
    artifact: Mapped[dict[str, Any]]  # содержимое artifact.json (метрики, пороги, хеши)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    shadow_at: Mapped[datetime | None]
    activated_at: Mapped[datetime | None]
    retired_at: Mapped[datetime | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class FeatureSnapshotRecord(Base):
    """Вектор признаков, по которому принималось решение (§6.3)."""

    __tablename__ = "feature_snapshots"
    __table_args__ = (UniqueConstraint("market_id", "decision_time", "feature_set_version"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    decision_time: Mapped[datetime]
    feature_set_version: Mapped[str]
    values: Mapped[dict[str, Any]]
    input_hash: Mapped[str]
    last_candle_open_time: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SignalRecord(Base):
    """Сигнал модели и решение политики. Один на (рынок, момент решения, версия модели)."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("market_id", "decision_time", "model_version"),
        CheckConstraint("decision in ('BUY', 'SELL', 'HOLD', 'EXIT')", name="decision"),
        CheckConstraint("model_role in ('active', 'shadow')", name="model_role"),
        CheckConstraint(
            "execution_status is null or execution_status in "
            "('executing', 'executed', 'blocked', 'declined', 'skipped', 'error', 'none')",
            name="execution_status",
        ),
        Index("ix_signals_decision_time", "decision_time"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    decision_time: Mapped[datetime]
    model_version: Mapped[str] = mapped_column(ForeignKey("model_versions.version"))
    model_role: Mapped[str]
    policy_version: Mapped[str]
    feature_snapshot_id: Mapped[int | None] = mapped_column(ForeignKey("feature_snapshots.id"))
    probabilities: Mapped[dict[str, Any] | None]
    expected_return_bps: Mapped[float | None]
    decision: Mapped[str]
    reason: Mapped[str]
    position_side: Mapped[str | None]
    horizon_candles: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    outcome_return: Mapped[float | None]
    outcome_label: Mapped[str | None]
    outcome_at: Mapped[datetime | None]
    # Исполнение active-сигнала (этап 12). null — ещё не обрабатывался; executing ставится
    # атомарно до первого обращения к бирже, поэтому сигнал не исполняется дважды.
    execution_status: Mapped[str | None]
    execution_detail: Mapped[str | None]
    execution_order_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("orders.id"))
    execution_started_at: Mapped[datetime | None]
    execution_finished_at: Mapped[datetime | None]
