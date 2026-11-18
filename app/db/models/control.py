from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Identity,
    Index,
    SmallInteger,
    false,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.config.settings import TradingMode
from app.db.base import Base

NETWORK_CHECK = "network in ('testnet', 'mainnet')"
_MODES = ", ".join(f"'{m.value}'" for m in TradingMode)


class BotStateRecord(Base):
    """Единственная строка с текущим режимом бота в этой БД (одна БД = одна сеть)."""

    __tablename__ = "bot_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(NETWORK_CHECK, name="network"),
        CheckConstraint(f"trading_mode in ({_MODES})", name="trading_mode"),
        CheckConstraint("not entries_blocked or block_reason is not null", name="block_reason"),
    )

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, server_default=text("1"))
    network: Mapped[str]
    trading_mode: Mapped[str]
    # Запрет новых входов независимо от режима (лимит убытка, устаревшие данные, расхождения).
    entries_blocked: Mapped[bool] = mapped_column(server_default=false())
    block_reason: Mapped[str | None]
    version: Mapped[int] = mapped_column(server_default=text("1"))
    updated_by: Mapped[str]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class AuditLogRecord(Base):
    """Журнал аудита. Только добавление: UPDATE и DELETE запрещены триггером в БД."""

    __tablename__ = "audit_log"
    __table_args__ = (Index(None, "occurred_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor: Mapped[str]
    action: Mapped[str]
    reason: Mapped[str | None]
    correlation_id: Mapped[str | None]
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class HeartbeatRecord(Base):
    """Последний heartbeat каждого компонента (collector, trader, ...)."""

    __tablename__ = "heartbeats"

    component: Mapped[str] = mapped_column(primary_key=True)
    instance_id: Mapped[str]
    started_at: Mapped[datetime]
    last_beat_at: Mapped[datetime]
    details: Mapped[dict[str, Any]] = mapped_column(server_default=text("'{}'::jsonb"))


class NonceStateRecord(Base):
    """Последний выданный nonce для каждого подписывающего API-кошелька.

    Nonce выдаётся атомарно в БД: max(текущее время в мс, последний + 1), поэтому
    повтор невозможен даже после рестарта или при ошибочном втором процессе.
    """

    __tablename__ = "nonce_state"

    signer: Mapped[str] = mapped_column(primary_key=True)
    last_nonce: Mapped[int] = mapped_column(BigInteger)
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())
