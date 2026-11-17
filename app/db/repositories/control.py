"""Состояние бота, аудит и heartbeat.

Каждая смена состояния пишет запись аудита в той же транзакции.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import ConfigurationError, Network, TradingMode
from app.db.models import AuditLogRecord, BotStateRecord, HeartbeatRecord
from app.domain.control import Actor, ActorKind, validate_transition
from app.observability.logging import current_correlation_id

SYSTEM_STARTUP = Actor(ActorKind.SYSTEM, "startup")
CONFIG_STARTUP = Actor(ActorKind.CONFIG, "startup")


async def record_audit(
    session: AsyncSession,
    *,
    actor: Actor,
    action: str,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    session.add(
        AuditLogRecord(
            actor=str(actor),
            action=action,
            reason=reason,
            correlation_id=current_correlation_id(),
            details=details or {},
        )
    )
    await session.flush()


async def _lock_state(session: AsyncSession) -> BotStateRecord | None:
    stmt = select(BotStateRecord).where(BotStateRecord.id == 1).with_for_update()
    return (await session.execute(stmt)).scalar_one_or_none()


async def get_bot_state(session: AsyncSession) -> BotStateRecord | None:
    return await session.get(BotStateRecord, 1)


async def ensure_bot_state(session: AsyncSession, network: Network) -> BotStateRecord:
    """Возвращает (создаёт при отсутствии) состояние; проверяет, что БД принадлежит этой сети."""
    state = await _lock_state(session)
    if state is None:
        state = BotStateRecord(
            id=1,
            network=network.value,
            trading_mode=TradingMode.DISABLED.value,
            updated_by=str(SYSTEM_STARTUP),
        )
        session.add(state)
        await session.flush()
        await record_audit(
            session,
            actor=SYSTEM_STARTUP,
            action="bot_state.created",
            details={"network": network.value, "trading_mode": TradingMode.DISABLED.value},
        )
    elif state.network != network.value:
        raise ConfigurationError(
            f"БД принадлежит сети {state.network}, а процесс запущен для {network.value}"
        )
    return state


async def set_trading_mode(
    session: AsyncSession,
    network: Network,
    target: TradingMode,
    *,
    actor: Actor,
    reason: str | None,
) -> bool:
    """Меняет режим по правилам app.domain.control. Возвращает False, если режим уже такой."""
    state = await ensure_bot_state(session, network)
    current = TradingMode(state.trading_mode)
    if current is target:
        return False
    validate_transition(network=network, current=current, target=target, actor=actor, reason=reason)
    state.trading_mode = target.value
    state.updated_by = str(actor)
    state.version += 1
    await record_audit(
        session,
        actor=actor,
        action="bot_state.mode_changed",
        reason=reason,
        details={"from": current.value, "to": target.value},
    )
    return True


async def block_entries(
    session: AsyncSession, network: Network, *, actor: Actor, reason: str
) -> bool:
    """Запрещает новые входы. Кто угодно может запретить; повторный вызов ничего не меняет."""
    state = await ensure_bot_state(session, network)
    if state.entries_blocked:
        return False
    state.entries_blocked = True
    state.block_reason = reason
    state.updated_by = str(actor)
    state.version += 1
    await record_audit(session, actor=actor, action="bot_state.entries_blocked", reason=reason)
    return True


async def unblock_entries(
    session: AsyncSession, network: Network, *, actor: Actor, reason: str
) -> bool:
    """Снимает запрет входов. Только оператор и только с причиной (ручной разбор)."""
    if actor.kind is not ActorKind.OPERATOR:
        raise PermissionError("снять запрет входов может только оператор")
    if not reason.strip():
        raise ValueError("нужна причина снятия запрета")
    state = await ensure_bot_state(session, network)
    if not state.entries_blocked:
        return False
    previous = state.block_reason
    state.entries_blocked = False
    state.block_reason = None
    state.updated_by = str(actor)
    state.version += 1
    await record_audit(
        session,
        actor=actor,
        action="bot_state.entries_unblocked",
        reason=reason,
        details={"previous_block_reason": previous},
    )
    return True


async def downgrade_after_restart(session: AsyncSession, network: Network) -> bool:
    """Старт торгового цикла: LIVE_ENABLED из прошлого запуска → LIVE_PAUSED (spec §6).
    Торговля после рестарта не возобновляется сама — LIVE_ENABLED включает оператор."""
    state = await ensure_bot_state(session, network)
    if state.trading_mode != TradingMode.LIVE_ENABLED.value:
        return False
    await set_trading_mode(
        session, network, TradingMode.LIVE_PAUSED, actor=SYSTEM_STARTUP, reason="restart trader"
    )
    return True


async def apply_startup_mode(
    session: AsyncSession, network: Network, configured: TradingMode
) -> BotStateRecord:
    """Вызывается при старте торгового процесса.

    1. LIVE_ENABLED из прошлого запуска понижается до LIVE_PAUSED (spec §6).
    2. Затем применяется TRADING_MODE из конфигурации.
    Запрет входов (entries_blocked) переживает рестарт и снимается только оператором.
    """
    state = await ensure_bot_state(session, network)
    if state.trading_mode == TradingMode.LIVE_ENABLED.value:
        await set_trading_mode(
            session, network, TradingMode.LIVE_PAUSED, actor=SYSTEM_STARTUP, reason="restart"
        )
    await set_trading_mode(
        session, network, configured, actor=CONFIG_STARTUP, reason="TRADING_MODE"
    )
    return state


async def beat(
    session: AsyncSession,
    *,
    component: str,
    instance_id: str,
    started_at: datetime,
    now: datetime,
    details: dict[str, Any] | None = None,
) -> None:
    values = {
        "component": component,
        "instance_id": instance_id,
        "started_at": started_at,
        "last_beat_at": now,
        "details": details or {},
    }
    stmt = insert(HeartbeatRecord).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=["component"],
        set_={k: stmt.excluded[k] for k in values if k != "component"},
    )
    await session.execute(stmt)


async def get_heartbeats(session: AsyncSession) -> list[HeartbeatRecord]:
    stmt = select(HeartbeatRecord).order_by(HeartbeatRecord.component)
    return list((await session.execute(stmt)).scalars())
