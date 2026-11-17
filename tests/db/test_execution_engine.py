"""Execution engine против симулятора Hyperliquid и настоящего PostgreSQL.

Сценарии критерия готовности этапа 6: вход, частичное/полное исполнение, отмена, отклонение,
закрытие и восстановление после таймаута — без дублирования ордеров.
"""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config.settings import Network, TradingMode
from app.db.models import AuditLogRecord
from app.db.repositories import control
from app.db.repositories import trading as trading_repo
from app.domain.account import Side
from app.domain.orders import OrderStatus
from tests.db.trading_env import NOW, OPERATOR, Env
from tests.execution.fake_hyperliquid import SimulatedCrashError

# --- вход и защита ---


async def test_entry_full_fill_sets_leverage_and_protective_stop(env: Env) -> None:
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)

    assert result.warnings == []
    entry = result.order
    assert entry is not None and entry.status is OrderStatus.FILLED
    assert entry.filled_size == Decimal("0.00023") and entry.avg_price == Decimal("65000.5")
    assert await env.statuses(entry.order_id) == ["CREATED", "APPROVED", "SIGNED", "FILLED"]

    # Предпросмотр, который видел оператор.
    [preview] = env.previews
    assert preview.limit_price == Decimal("65065")  # mid × (1 + 10 bps), округление вниз
    assert preview.leverage_target == 2 and preview.stop_trigger == Decimal("64675")

    # Плечо выставлено и записано в аудит.
    assert (env.fake.leverage_type, env.fake.leverage) == ("isolated", 2)
    async with env.sm() as db:
        actions = [a.action for a in (await db.execute(select(AuditLogRecord))).scalars()]
    assert "leverage.updated" in actions

    # Stop: reduceOnly, на исполненный размер, от фактической цены входа, стоит на бирже.
    stop = result.stop
    assert stop is not None and stop.status is OrderStatus.RESTING
    stop_order = await env.order(stop.order_id)
    assert stop_order.reduce_only and stop_order.side == "sell"
    assert stop_order.size == Decimal("0.00023")
    assert stop_order.trigger_price == Decimal("64676")  # 65000.5 − 325, округление к входу
    assert stop_order.price == Decimal("61442")  # худшая цена: −5%
    assert stop_order.parent_order_id == entry.order_id
    assert len(env.order_actions()) == 2  # вход + стоп, без дублей


async def test_partial_fill_protects_only_filled_size(env: Env) -> None:
    env.fake.liquidity = Decimal("0.0002")
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.PARTIALLY_FILLED
    assert result.order.filled_size == Decimal("0.0002")
    assert result.stop is not None
    assert (await env.order(result.stop.order_id)).size == Decimal("0.0002")


async def test_short_entry_stop_above(env: Env) -> None:
    result = await env.engine.open_position(Side.SELL, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.FILLED
    assert env.fake.position == Decimal("-0.00023")
    assert result.stop is not None
    stop = await env.order(result.stop.order_id)
    assert stop.side == "buy" and stop.trigger_price == Decimal("65324")  # 64999.5 + 325 ↓


async def test_ioc_without_liquidity_is_rejected_without_position(env: Env) -> None:
    env.fake.liquidity = Decimal(0)
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.REJECTED
    assert result.stop is None and env.fake.position == 0
    assert (await env.order(result.order.order_id)).error_code == "ioc_no_match"


async def test_exchange_rejection_is_recorded(env: Env) -> None:
    env.fake.reject_next_order = "Insufficient margin to place order."
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.REJECTED
    order = await env.order(result.order.order_id)
    assert order.error_code == "margin" and order.response is not None


# --- guard и оператор ---


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ("mode_disabled", "не разрешает новые входы"),
        ("entries_blocked", "новые входы запрещены"),
        ("too_big", "больше лимита"),
        ("too_small", "меньше минимума"),
        ("wide_spread", "шире лимита"),
        ("no_margin", "доступно"),
    ],
)
async def test_guard_blocks_before_anything_is_sent(env: Env, setup: str, reason: str) -> None:
    notional = Decimal("15")
    if setup == "mode_disabled":
        await env.set_mode(TradingMode.DISABLED)
    elif setup == "entries_blocked":
        async with env.sm.begin() as db:
            await control.block_entries(db, Network.TESTNET, actor=OPERATOR, reason="test block")
    elif setup == "too_big":
        notional = Decimal("50")
    elif setup == "too_small":
        notional = Decimal("5")
    elif setup == "wide_spread":
        env.fake.bid, env.fake.ask = Decimal("64900"), Decimal("65100")
    elif setup == "no_margin":
        env.fake.available = Decimal("1")

    result = await env.engine.open_position(Side.BUY, notional, confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.BLOCKED
    assert any(reason in r for r in result.order.guard_reasons)
    assert env.fake.exchange_actions == []  # ни плечо, ни ордер не отправлялись
    assert env.previews == []  # оператору не предлагали подтвердить заведомо запрещённое


async def test_operator_decline_sends_nothing(env: Env) -> None:
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.no)
    assert result.order is not None and result.order.status is OrderStatus.BLOCKED
    assert env.fake.exchange_actions == [] and env.fake.leverage == 12


async def test_manual_confirmation_is_mandatory(env: Env) -> None:
    with pytest.raises(Exception, match="ручное подтверждение"):
        await env.engine.open_position(Side.BUY, Decimal("15"), confirm=None)


async def test_no_pyramiding(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    second = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert second.order is not None and second.order.status is OrderStatus.BLOCKED
    assert any("позиция уже открыта" in r for r in second.order.guard_reasons)


# --- сбои связи: без слепых повторов ---


async def test_response_lost_after_fill_is_recovered_without_duplicate(env: Env) -> None:
    env.fake.leverage = 2  # плечо уже верное — первое действие на бирже будет ордером
    env.fake.fail_next = "timeout_after"
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)

    assert result.order is not None and result.order.status is OrderStatus.FILLED
    assert await env.statuses(result.order.order_id) == [
        "CREATED",
        "APPROVED",
        "SIGNED",
        "UNKNOWN",
        "FILLED",
    ]
    entries = [a for a in env.order_actions() if not a["orders"][0]["r"]]
    assert len(entries) == 1  # вход отправлен ровно один раз
    assert result.stop is not None and result.stop.status is OrderStatus.RESTING


async def test_request_lost_before_processing_resolves_to_rejected_after_timeout(env: Env) -> None:
    env.fake.leverage = 2
    env.fake.fail_next = "timeout_before"
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.UNKNOWN
    assert result.stop is None and env.fake.position == 0

    # До истечения max_unknown_order_seconds ордер остаётся UNKNOWN — повтора нет.
    env.clock.now += timedelta(seconds=10)
    assert (await env.engine.resolve(result.order.order_id)).status is OrderStatus.UNKNOWN
    env.clock.now += timedelta(seconds=25)
    resolved = await env.engine.resolve(result.order.order_id)
    assert resolved.status is OrderStatus.REJECTED
    assert env.order_actions() == []  # ни одного ордера на бирже


async def test_connection_refused_is_not_sent(env: Env) -> None:
    env.fake.leverage = 2
    env.fake.fail_next = "connect"
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.NOT_SENT


async def test_crash_after_send_is_recovered_on_restart(env: Env) -> None:
    env.fake.leverage = 2
    env.fake.fail_next = "crash_after"
    with pytest.raises(SimulatedCrashError):
        await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    async with env.sm() as db:
        [order] = await trading_repo.list_orders(db, statuses=[OrderStatus.SIGNED])
    assert order.nonce is not None and order.request_action is not None  # след сохранён до отправки

    # «Рестарт»: сверка находит исполненный ордер по cloid.
    [resolved] = await env.engine.resolve_unresolved()
    assert resolved.status is OrderStatus.FILLED and resolved.filled_size == order.size
    assert len(env.order_actions()) == 1


async def test_unknown_response_format_blocks_new_entries(env: Env) -> None:
    env.fake.leverage = 2
    env.fake.weird_next_response = {
        "status": "ok",
        "response": {"type": "order", "data": {"weird": 1}},
    }
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None
    # Сверка по cloid нашла реальный исход, но новые входы заблокированы до ручного разбора.
    assert result.order.status is OrderStatus.FILLED
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked
    assert "ошибка интеграции" in (state.block_reason or "")


# --- проблемы с защитой ---


async def test_rejected_stop_blocks_entries(env: Env) -> None:
    env.fake.reject_trigger = "Invalid TP/SL price."
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.FILLED
    assert result.stop is not None and result.stop.status is OrderStatus.REJECTED
    assert any("ПОЗИЦИЯ БЕЗ ЗАЩИТЫ" in w for w in result.warnings)
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked


async def test_stop_accepted_without_oid_is_resolved_by_cloid(env: Env) -> None:
    env.fake.trigger_status_string = True
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.warnings == []
    assert result.stop is not None and result.stop.status is OrderStatus.RESTING


# --- закрытие и отмена ---


async def test_close_position_cancels_stop(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    closed = await env.engine.close_position(confirm=env.yes)

    assert closed.order is not None and closed.order.status is OrderStatus.FILLED
    exit_order = await env.order(closed.order.order_id)
    assert exit_order.reduce_only and exit_order.side == "sell"
    assert env.fake.position == 0
    assert (await env.order(opened.stop.order_id)).status == OrderStatus.CANCELED.value


async def test_partial_close_replaces_stop_with_remaining_size(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    env.fake.liquidity = Decimal("0.0001")
    closed = await env.engine.close_position(confirm=env.yes)

    assert closed.order is not None and closed.order.status is OrderStatus.PARTIALLY_FILLED
    assert env.fake.position == Decimal("0.00013")
    assert (await env.order(opened.stop.order_id)).status == OrderStatus.CANCELED.value
    assert closed.stop is not None and closed.stop.status is OrderStatus.RESTING
    new_stop = await env.order(closed.stop.order_id)
    assert new_stop.size == Decimal("0.00013") and new_stop.trigger_price == Decimal("64676")


async def test_close_without_position(env: Env) -> None:
    result = await env.engine.close_position(confirm=env.yes)
    assert result.order is None and result.warnings == ["позиции нет"]


async def test_close_blocked_when_trading_disabled(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    await env.set_mode(TradingMode.DISABLED)
    blocked = await env.engine.close_position(confirm=env.yes)
    assert blocked.order is not None and blocked.order.status is OrderStatus.BLOCKED


async def test_cancel_resting_stop(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    canceled = await env.engine.cancel(opened.stop.order_id)
    assert canceled.status is OrderStatus.CANCELED
    assert await env.statuses(opened.stop.order_id) == [
        "CREATED",
        "APPROVED",
        "SIGNED",
        "RESTING",
        "CANCELED",
    ]


async def test_triggered_stop_is_reconciled_and_fills_synced(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    env.fake.trigger_stop()
    resolved = await env.engine.resolve_unresolved()
    stop = next(r for r in resolved if r.order_id == opened.stop.order_id)
    assert stop is not None and stop.status is OrderStatus.FILLED
    assert env.fake.position == 0

    new = await env.engine.sync_fills(NOW - timedelta(hours=1))
    assert new >= 1  # исполнение стопа (и входа, если ещё не было) сохранено
    assert await env.engine.sync_fills(NOW - timedelta(hours=1)) == 0  # дедупликация по tid
    await env.engine.snapshot()
