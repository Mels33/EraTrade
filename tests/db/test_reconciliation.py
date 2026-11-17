"""Сверка с биржей и восстановление после сбоев (§11 плана) — сценарии §11.3."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select, update

from app.config.settings import Network, TradingMode
from app.db.models import BotStateRecord, OrderEventRecord, OrderRecord, ReconciliationRunRecord
from app.db.repositories import control
from app.db.repositories import reconciliation as rec_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.domain.orders import OrderKind, OrderPurpose, OrderStatus
from app.execution.guard import ProposedOrder
from app.reconciliation.reconciler import DiffKind, ReconciliationReport, Severity
from tests.db.trading_env import OPERATOR, Env
from tests.execution.fake_hyperliquid import SimulatedCrashError

SIZE = Decimal("0.00023")


def kinds(report: ReconciliationReport, severity: Severity | None = None) -> set[DiffKind]:
    return {d.kind for d in report.differences if severity is None or d.severity is severity}


async def entries_blocked(env: Env) -> tuple[bool, str | None]:
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None
    return state.entries_blocked, state.block_reason


async def test_clean_state_after_round_trip(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    await env.engine.close_position(confirm=env.yes)
    report = await env.reconciler().run("manual")

    assert report.status == "ok", report.differences
    assert report.exchange_position == report.expected_position == 0
    assert report.critical == [] and not report.entries_blocked
    async with env.sm() as db:
        run = await db.get(ReconciliationRunRecord, report.run_id)
    assert run is not None and run.status == "ok" and run.finished_at is not None


async def test_open_position_with_stop_after_restart_is_ok(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    report = await env.reconciler().run("startup")
    assert report.status == "ok", report.differences
    assert report.exchange_position == report.expected_position == SIZE


async def test_abandoned_intent_is_closed_safely(env: Env) -> None:
    env.fake.liquidity = Decimal(0)
    # Намерение, которое «процесс» создал и не успел подписать.
    order = await env.engine._create(
        ProposedOrder(
            OrderPurpose.ENTRY,
            OrderKind.LIMIT_IOC,
            Side.BUY,
            SIZE,
            Decimal("65065"),
            reduce_only=False,
        ),
        intent={},
    )
    report = await env.reconciler().run("startup")
    assert DiffKind.ABANDONED_INTENT not in kinds(report)  # ещё свежее — не трогаем

    async with env.sm.begin() as db:  # created_at ставит сервер БД — сдвигаем его в прошлое
        await db.execute(
            update(OrderRecord)
            .where(OrderRecord.id == order.id)
            .values(created_at=func.now() - timedelta(minutes=10))
        )
    report = await env.reconciler().run("startup")
    assert DiffKind.ABANDONED_INTENT in kinds(report, Severity.INFO)
    assert (await env.order(order.id)).status == OrderStatus.BLOCKED.value
    assert env.order_actions() == []


async def test_crash_after_entry_resolves_order_and_flags_missing_stop(env: Env) -> None:
    env.fake.leverage = 2
    env.fake.fail_next = "crash_after"
    with pytest.raises(SimulatedCrashError):
        await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)

    report = await env.reconciler().run("startup")

    assert DiffKind.ORDER_RESOLVED in kinds(report, Severity.INFO)  # SIGNED → FILLED по cloid
    assert report.exchange_position == report.expected_position == SIZE  # позиция объяснена
    assert DiffKind.UNPROTECTED_POSITION in kinds(report, Severity.CRITICAL)  # стоп не успел встать
    assert report.status == "critical"
    blocked, reason = await entries_blocked(env)
    assert blocked and "без биржевого стопа" in (reason or "")
    # Новые входы теперь отклоняются guard.
    retry = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert retry.order is not None and retry.order.status is OrderStatus.BLOCKED


async def test_stop_on_exchange_with_lost_local_record(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    async with env.sm.begin() as db:  # локальная запись о стопе потеряна
        await db.execute(
            delete(OrderEventRecord).where(OrderEventRecord.order_id == opened.stop.order_id)
        )
        await db.execute(delete(OrderRecord).where(OrderRecord.id == opened.stop.order_id))

    report = await env.reconciler().run("startup")
    assert DiffKind.FOREIGN_ORDER in kinds(report, Severity.WARNING)
    assert DiffKind.UNPROTECTED_POSITION not in kinds(report)  # защита на бирже есть
    assert report.status == "warnings" and not report.entries_blocked


async def test_manual_close_via_exchange_ui(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    env.fake.manual_fill(is_buy=False, size=SIZE)  # закрыли руками в интерфейсе Hyperliquid

    report = await env.reconciler().run("periodic")
    assert report.exchange_position == 0 and report.expected_position == SIZE
    assert DiffKind.POSITION_MISMATCH in kinds(report, Severity.CRITICAL)
    assert DiffKind.FOREIGN_FILLS in kinds(report, Severity.WARNING)
    assert (await env.order(opened.stop.order_id)).status == OrderStatus.CANCELED.value
    assert (await entries_blocked(env))[0]

    # Оператор признаёт состояние биржи: расхождение уходит, запрет остаётся до разблокировки.
    accepted = await env.reconciler().accept_exchange_position(
        actor=OPERATOR, reason="закрыл руками"
    )
    assert accepted == 0
    report = await env.reconciler().run("manual")
    assert report.critical == [] and report.expected_position == 0
    assert (await entries_blocked(env))[0]
    with pytest.raises(PermissionError):
        await env.reconciler().accept_exchange_position(
            actor=Actor(ActorKind.SYSTEM, "x"), reason="auto"
        )


async def test_stop_canceled_by_exchange(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    stop = await env.order(opened.stop.order_id)
    assert stop.oid is not None
    env.fake.exchange_cancels(stop.oid, "marginCanceled")

    report = await env.reconciler().run("periodic")
    assert (await env.order(stop.id)).status == OrderStatus.CANCELED.value
    assert DiffKind.ORDER_RESOLVED in kinds(report)
    assert DiffKind.UNPROTECTED_POSITION in kinds(report, Severity.CRITICAL)


async def test_triggered_stop_fill_is_synced_and_explained(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    env.fake.trigger_stop()  # бот не видел исполнения стопа
    report = await env.reconciler().run("periodic")
    assert DiffKind.ORDER_RESOLVED in kinds(report)
    assert report.exchange_position == report.expected_position == 0
    assert report.status == "ok", report.differences


async def test_position_bigger_than_stop(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    env.fake.manual_fill(is_buy=True, size=Decimal("0.0001"))
    report = await env.reconciler().run("periodic")
    assert {DiffKind.POSITION_MISMATCH, DiffKind.UNDERSIZED_STOP} <= kinds(
        report, Severity.CRITICAL
    )


async def test_funding_and_ledger_are_synced_once(env: Env) -> None:
    env.fake.add_funding("-0.0012")
    env.fake.add_deposit("50.0")
    report = await env.reconciler().run("periodic")
    [events] = [d for d in report.differences if d.kind is DiffKind.ACCOUNT_EVENTS]
    assert events.details == {"funding": 1, "ledger": 1}

    report = await env.reconciler().run("periodic")
    assert DiffKind.ACCOUNT_EVENTS not in kinds(report)  # без дублей


async def test_critical_difference_pauses_live_trading(env: Env) -> None:
    # Состояние mainnet в LIVE_ENABLED → критичное расхождение понижает до LIVE_PAUSED.
    async with env.sm.begin() as db:
        await db.execute(delete(BotStateRecord))
        for mode in (TradingMode.LIVE_PAUSED, TradingMode.LIVE_ENABLED):
            await control.set_trading_mode(db, Network.MAINNET, mode, actor=OPERATOR, reason="t")
    env.fake.manual_fill(is_buy=True, size=Decimal("0.0002"))  # позиция без стопа и не от бота

    reconciler = env.reconciler()
    reconciler.network = Network.MAINNET
    report = await reconciler.run("periodic")
    assert report.status == "critical"
    async with env.sm() as db:
        state = await control.get_bot_state(db)
        differences = await rec_repo.list_differences(db, report.run_id)
    assert state is not None and state.trading_mode == TradingMode.LIVE_PAUSED.value
    assert state.updated_by == "system:reconciler" and state.entries_blocked
    assert {d.kind for d in differences} >= {"position_mismatch", "unprotected_position"}


async def test_failed_run_is_recorded_as_error(env: Env) -> None:
    reconciler = env.reconciler()
    reconciler.user = "not-an-address"
    with pytest.raises(ValueError):
        await reconciler.run("manual")
    async with env.sm() as db:
        run = (
            (
                await db.execute(
                    select(ReconciliationRunRecord).order_by(ReconciliationRunRecord.id.desc())
                )
            )
            .scalars()
            .first()
        )
    assert run is not None and run.status == "error" and "ValueError" in run.summary["error"]
