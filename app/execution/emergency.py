"""Аварийные команды (§12.5 плана): PAUSE, CANCEL_ALL, FLATTEN, FULL_KILL.

Отличие от плана: FULL_KILL сначала закрывает позицию. Иначе отмена ордеров сняла бы
защитный стоп и оставила позицию без защиты до ручного разбора.
"""

from dataclasses import dataclass, field
from decimal import Decimal

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.db.repositories import control
from app.domain.control import Actor
from app.execution.engine import Confirm, ExecutionEngine, TradeResult
from app.integrations.hyperliquid import actions
from app.observability.notify import Notifier


@dataclass
class EmergencyResult:
    steps: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    trade: TradeResult | None = None


async def pause(
    sm: async_sessionmaker[AsyncSession],
    network: Network,
    *,
    actor: Actor,
    reason: str,
    notifier: Notifier,
) -> list[str]:
    """Запрет новых входов; LIVE_ENABLED → LIVE_PAUSED. Закрытие и стопы остаются доступны."""
    steps = []
    async with sm.begin() as db:
        if await control.block_entries(db, network, actor=actor, reason=f"PAUSE: {reason}"):
            steps.append("новые входы запрещены")
        state = await control.ensure_bot_state(db, network)
        if state.trading_mode == TradingMode.LIVE_ENABLED.value:
            await control.set_trading_mode(
                db, network, TradingMode.LIVE_PAUSED, actor=actor, reason=f"PAUSE: {reason}"
            )
            steps.append("режим LIVE_ENABLED → LIVE_PAUSED")
    await notifier.send(f"PAUSE ({actor}): {reason}")
    return steps or ["уже на паузе"]


async def cancel_all(engine: ExecutionEngine, *, actor: Actor, reason: str) -> EmergencyResult:
    """Отменяет все открытые ордера инструмента на бирже (включая не созданные ботом)."""
    result = EmergencyResult()
    orders = [o for o in await engine.gateway.get_open_orders(engine.user) if o.coin == engine.coin]
    for order in orders:
        response = await engine.session.submit(
            actions.cancel_action(engine.rules.asset_id, order.oid)
        )
        statuses = (response.data or {}).get("statuses", []) if response.ok else []
        ok = response.ok and statuses == ["success"]
        outcome = "ok" if ok else (response.error or str(statuses))
        result.steps.append(f"отмена oid {order.oid} ({order.order_type}): {outcome}")
    await engine.tracker.resolve_unresolved()
    state = await engine.gateway.get_account_state(engine.user)
    position = state.position(engine.coin)
    if position is not None and position.size != 0:
        result.warnings.append(f"позиция {position.size} осталась БЕЗ стопа")
    async with engine.sm.begin() as db:
        await control.record_audit(
            db,
            actor=actor,
            action="emergency.cancel_all",
            reason=reason,
            details={"canceled": [o.oid for o in orders]},
        )
    if not orders:
        result.steps.append("открытых ордеров нет")
    return result


async def flatten(engine: ExecutionEngine, *, confirm: Confirm | None) -> EmergencyResult:
    """Закрывает позицию reduceOnly-ордером и снимает стопы."""
    trade = await engine.close_position(confirm=confirm)
    result = EmergencyResult(trade=trade, warnings=list(trade.warnings))
    if trade.order is not None:
        result.steps.append(
            f"закрытие: {trade.order.status.value}, исполнено {trade.order.filled_size}"
        )
    return result


async def full_kill(
    engine: ExecutionEngine,
    *,
    actor: Actor,
    reason: str,
    confirm: Confirm | None,
) -> EmergencyResult:
    """PAUSE → FLATTEN → CANCEL_ALL → DISABLED. Дальнейшая работа — только после разбора."""
    result = EmergencyResult()
    result.steps += await pause(
        engine.sm, engine.network, actor=actor, reason=f"KILL: {reason}", notifier=engine.notifier
    )
    state = await engine.gateway.get_account_state(engine.user)
    position = state.position(engine.coin)
    if position is not None and position.size != Decimal(0):
        flat = await flatten(engine, confirm=confirm)
        result.steps += flat.steps
        result.warnings += flat.warnings
        result.trade = flat.trade
    canceled = await cancel_all(engine, actor=actor, reason=f"KILL: {reason}")
    result.steps += canceled.steps
    result.warnings += canceled.warnings
    async with engine.sm.begin() as db:
        await control.set_trading_mode(
            db, engine.network, TradingMode.DISABLED, actor=actor, reason=f"KILL: {reason}"
        )
        await control.record_audit(
            db,
            actor=actor,
            action="emergency.full_kill",
            reason=reason,
            details={"steps": result.steps, "warnings": result.warnings},
        )
    result.steps.append("режим → DISABLED")
    logfire.error("FULL_KILL: {reason}", reason=reason, steps=result.steps)
    await engine.notifier.send(
        f"FULL_KILL ({actor}): {reason}. " + "; ".join(result.steps + result.warnings)
    )
    return result
