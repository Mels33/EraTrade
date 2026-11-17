"""Risk engine (лимиты убытка и сделок), аварийные команды и dead man's switch (§12)."""

import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.config.settings import Network, Settings, TradingMode
from app.config.strategy import DeadMansSwitchConfig
from app.db.models import AuditLogRecord, FillRecord
from app.db.repositories import control
from app.domain.account import Side
from app.domain.orders import OrderStatus
from app.execution import emergency
from app.execution.dead_mans_switch import DeadMansSwitch
from app.observability.notify import LogNotifier, TelegramNotifier, build_notifier
from app.reconciliation.reconciler import DiffKind, Severity
from tests.db.trading_env import NOW, OPERATOR, Env


async def add_fill(env: Env, *, closed_pnl: str, fee: str = "0.01", hours_ago: float = 1) -> None:
    async with env.sm.begin() as db:
        tid = 10_000 + len((await db.execute(select(FillRecord.tid))).all())
        db.add(
            FillRecord(
                tid=tid,
                oid=tid,
                market_id=env.engine.market_id,
                coin="BTC",
                side="sell",
                price=Decimal("65000"),
                size=Decimal("0.0003"),
                fee=Decimal(fee),
                fee_token="USDC",
                closed_pnl=Decimal(closed_pnl),
                direction="Close Long",
                start_position=Decimal("0.0003"),
                hash="0x" + "1" * 64,
                time=NOW - timedelta(hours=hours_ago),
                raw={},
            )
        )


async def blocked(env: Env) -> tuple[bool, str | None, str]:
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    assert state is not None
    return state.entries_blocked, state.block_reason, state.trading_mode


# --- risk engine ---


async def test_snapshot_pnl_components(env: Env) -> None:
    await add_fill(env, closed_pnl="-1.5", fee="0.02")
    await add_fill(env, closed_pnl="0.5", fee="0.01", hours_ago=24 * 3)  # суббота прошлой недели
    env.fake.add_funding("-0.03")
    await env.reconciler().run("manual")  # синхронизирует funding

    snap = await env.engine.risk.snapshot()
    assert snap.equity == Decimal("98.45")
    assert snap.realized_today == Decimal("-1.5") and snap.fees_today == Decimal("0.02")
    assert snap.funding_today == Decimal("-0.03")
    assert snap.daily_pnl == Decimal("-1.55")
    # NOW — вторник 17.11.2026; 3 дня назад — суббота прошлой недели, в недельный PnL не входит.
    assert snap.weekly_pnl == Decimal("-1.55")


async def test_daily_loss_limit_blocks_entry_without_sending(env: Env) -> None:
    await add_fill(env, closed_pnl="-3")  # 3 USDC ≥ 2% от ≈101.4
    result = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert result.order is not None and result.order.status is OrderStatus.BLOCKED
    assert any("дневной убыток" in r for r in result.order.guard_reasons)
    assert env.fake.exchange_actions == []


async def test_trades_per_day_limit(env: Env) -> None:
    for _ in range(4):
        await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
        await env.engine.close_position(confirm=env.yes)
    fifth = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert fifth.order is not None and fifth.order.status is OrderStatus.BLOCKED
    assert any("лимит сделок" in r for r in fifth.order.guard_reasons)
    # Это не устойчивый запрет: на следующий день входы снова разрешены.
    assert not (await blocked(env))[0]
    env.clock.now += timedelta(days=1)
    nxt = await env.engine.open_position(
        Side.BUY, Decimal("15"), confirm=env.yes, stop_price=Decimal("64600")
    )
    assert nxt.order is not None and nxt.order.status is OrderStatus.FILLED


async def test_loss_limit_enforced_by_reconciler_persists_and_notifies_once(env: Env) -> None:
    await add_fill(env, closed_pnl="-3")
    report = await env.reconciler().run("periodic")
    assert DiffKind.LOSS_LIMIT in {d.kind for d in report.critical}
    is_blocked, reason, _ = await blocked(env)
    assert is_blocked and "дневной убыток" in (reason or "")
    assert len(env.notifier.messages) == 1

    await env.reconciler().run("periodic")
    assert len(env.notifier.messages) == 1  # без спама каждую минуту

    # На следующий день дневной PnL обнулился, но запрет остаётся до решения оператора.
    env.clock.now += timedelta(days=1)
    report = await env.reconciler().run("periodic")
    assert DiffKind.LOSS_LIMIT not in {d.kind for d in report.differences}
    assert (await blocked(env))[0]


async def test_unrealized_loss_counts_and_blocks(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    env.fake.unrealized_pnl = Decimal("-2.5")
    snap = await env.engine.risk.snapshot()
    assert snap.unrealized_pnl == Decimal("-2.5")
    assert snap.daily_pnl == Decimal("-2.5") - snap.fees_today + snap.realized_today
    # Нереализованный убыток открытой позиции тоже доводит до дневного лимита (2.5 ≥ 2%).
    report = await env.reconciler().run("periodic")
    assert DiffKind.LOSS_LIMIT in {d.kind for d in report.critical}


async def test_address_rate_limit_warning(env: Env) -> None:
    env.fake.requests_used = env.fake.requests_cap - 10
    report = await env.reconciler().run("periodic")
    assert DiffKind.ADDRESS_RATE_LIMIT in {
        d.kind for d in report.differences if d.severity is Severity.WARNING
    }


# --- аварийные команды ---


async def test_pause_blocks_and_downgrades(env: Env) -> None:
    steps = await emergency.pause(
        env.sm, Network.TESTNET, actor=OPERATOR, reason="проверка", notifier=env.notifier
    )
    assert "новые входы запрещены" in steps
    assert (await blocked(env))[0]
    assert env.notifier.messages and "PAUSE" in env.notifier.messages[0]


async def test_cancel_all_warns_about_unprotected_position(env: Env) -> None:
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    result = await emergency.cancel_all(env.engine, actor=OPERATOR, reason="тест")
    assert any("ok" in step for step in result.steps)
    assert any("БЕЗ стопа" in w for w in result.warnings)
    assert (await env.order(opened.stop.order_id)).status == OrderStatus.CANCELED.value


async def test_full_kill_flattens_cancels_and_disables(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    env.fake.external_stop(is_buy=False, size=Decimal("0.0001"), trigger="60000")  # чужой ордер

    result = await emergency.full_kill(env.engine, actor=OPERATOR, reason="авария", confirm=env.yes)

    assert env.fake.position == 0
    assert not [o for o in env.fake.orders.values() if o["status"] == "open"]
    is_blocked, _, mode = await blocked(env)
    assert is_blocked and mode == TradingMode.DISABLED.value
    assert result.warnings == []
    async with env.sm() as db:
        actions = [a.action for a in (await db.execute(select(AuditLogRecord))).scalars()]
    assert "emergency.full_kill" in actions
    assert any("FULL_KILL" in m for m in env.notifier.messages)


# --- dead man's switch ---


async def test_dead_mans_switch_arm_and_disarm(env: Env) -> None:
    config = DeadMansSwitchConfig(enabled=True, horizon_seconds=60, refresh_seconds=20)
    dms = DeadMansSwitch(env.engine.session, config, clock=env.clock)
    at = await dms.arm()
    assert at == NOW + timedelta(seconds=60)
    assert env.fake.scheduled_cancel_ms is not None
    await dms.disarm()
    assert env.fake.scheduled_cancel_ms is None


async def test_dead_mans_switch_cancels_protective_stop_when_it_fires(env: Env) -> None:
    # Почему он выключен по умолчанию: срабатывание снимает и защитный стоп.
    opened = await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    assert opened.stop is not None
    config = DeadMansSwitchConfig(enabled=True, horizon_seconds=60, refresh_seconds=20)
    await DeadMansSwitch(env.engine.session, config, clock=env.clock).arm()
    env.fake.advance(61_000)  # бот «умер» и не продлил
    report = await env.reconciler().run("periodic")
    assert DiffKind.UNPROTECTED_POSITION in {d.kind for d in report.critical}


async def test_disabled_dead_mans_switch_does_nothing(env: Env) -> None:
    config = DeadMansSwitchConfig(enabled=False, horizon_seconds=60, refresh_seconds=20)
    await DeadMansSwitch(env.engine.session, config).run()
    assert env.fake.exchange_actions == []


# --- уведомления ---


async def test_telegram_notifier_sends_without_leaking_token() -> None:
    sent: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append({"url": str(request.url), "body": json.loads(request.content)})
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "123:SECRET", "42", prefix="🔴 EraTrade MAINNET:", transport=httpx.MockTransport(handler)
    )
    assert await notifier.send("дневной лимит")
    assert sent[0]["body"] == {"chat_id": "42", "text": "🔴 EraTrade MAINNET: дневной лимит"}
    assert "SECRET" not in repr(notifier)


async def test_telegram_failure_does_not_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    notifier = TelegramNotifier("t", "1", prefix="x", transport=httpx.MockTransport(handler))
    assert not await notifier.send("ok")  # не бросает, сообщает о неудаче


def test_build_notifier_falls_back_to_log() -> None:
    base: dict[str, Any] = {
        "environment": "testnet",
        "hyperliquid_network": "testnet",
        "database_url": "postgresql+asyncpg://u:p@h/eratrade_testnet",
    }
    assert isinstance(build_notifier(Settings(_env_file=None, **base)), LogNotifier)
    configured = Settings(_env_file=None, telegram_bot_token="1:x", telegram_chat_id="5", **base)
    assert isinstance(build_notifier(configured), TelegramNotifier)


@pytest.mark.parametrize("bad", [{"horizon_seconds": 10, "refresh_seconds": 8}])
def test_dead_mans_switch_config_validation(bad: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        DeadMansSwitchConfig(enabled=True, **bad)
