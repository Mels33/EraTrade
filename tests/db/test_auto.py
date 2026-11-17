"""Этап 15: автоматическое исполнение — барьер, автопауза, рестарт без возобновления торговли."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Network, TradingMode
from app.config.strategy import StrategyConfig
from app.db.repositories import control
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.live import review
from app.live.auto import auto_failures
from app.live.readiness import Check, CheckStatus
from app.reconciliation.reconciler import (
    Difference,
    DiffKind,
    ReconciliationReport,
    Severity,
)
from app.signals.policy import Decision
from app.workers import trader as trader_module
from app.workers.trader import CycleConfig, TradingCycle
from tests.db.test_trading_cycle import DECISION, PARAMS, UP, StubCache, StubPredictor, prepare
from tests.db.trading_env import Env

OPERATOR = Actor(ActorKind.OPERATOR, "tester")


def auto_strategy(base: StrategyConfig, **auto: object) -> StrategyConfig:
    return base.model_copy(
        update={
            "execution": base.execution.model_copy(update={"require_manual_confirmation": False}),
            "auto": base.auto.model_copy(update={"enabled": True, **auto}),
        }
    )


def report_with(*diffs: Difference) -> ReconciliationReport:
    return ReconciliationReport(
        run_id=1,
        status="warnings" if diffs else "ok",
        differences=diffs,
        exchange_position=Decimal(0),
        expected_position=Decimal(0),
        entries_blocked=False,
    )


WARNING = Difference(DiffKind.FOREIGN_FILLS, Severity.WARNING, "чужие исполнения на аккаунте")


def auto_cycle(env: Env, cache: StubCache, report: ReconciliationReport) -> TradingCycle:
    async def reconcile() -> ReconciliationReport:
        return report

    async def no_sleep(_: float) -> None:
        env.clock.now += timedelta(seconds=2)

    return TradingCycle(
        env.engine,
        cache,
        params=PARAMS,
        interval="1h",
        step=PARAMS.step,
        max_delay=timedelta(seconds=90),
        confirm=None,
        reconcile=reconcile,
        config=CycleConfig(candle_wait=4, poll_interval=2),
        sleep=no_sleep,
        auto=True,
    )


async def blocked(env: Env) -> bool:
    async with env.sm() as db:
        state = await control.get_bot_state(db)
    return state is not None and state.entries_blocked


async def test_restart_downgrades_live_enabled(session: AsyncSession) -> None:
    await control.set_trading_mode(
        session, Network.MAINNET, TradingMode.LIVE_PAUSED, actor=OPERATOR, reason="r"
    )
    await control.set_trading_mode(
        session, Network.MAINNET, TradingMode.LIVE_ENABLED, actor=OPERATOR, reason="live"
    )
    assert await control.downgrade_after_restart(session, Network.MAINNET)
    state = await control.get_bot_state(session)
    assert state is not None and state.trading_mode == "LIVE_PAUSED"
    assert not await control.downgrade_after_restart(session, Network.MAINNET)  # уже не live


async def test_gate_refuses_by_default(env: Env) -> None:
    async with env.sm() as db:
        failures = await auto_failures(db, env.engine.market_id, env.engine.strategy, [])
    text = " | ".join(failures)
    assert "auto.enabled = false" in text
    assert "require_manual_confirmation = true" in text
    assert "закрытых сделок бота 0 из нужных 10" in text


async def test_gate_passes_after_clean_manual_stage(env: Env) -> None:
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    await env.engine.close_position(confirm=env.yes)
    await env.engine.sync_fills(env.clock() - timedelta(hours=1))
    strategy = auto_strategy(env.engine.strategy, min_manual_trades=1)
    fail = [Check("сеть", CheckStatus.FAIL, "testnet")]
    async with env.sm() as db:
        assert await auto_failures(db, env.engine.market_id, strategy, []) == []
        with_fail = await auto_failures(db, env.engine.market_id, strategy, fail)
    assert with_fail == ["live-check: сеть — testnet"]


async def test_auto_executes_without_confirmation(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    env.engine.strategy = auto_strategy(env.engine.strategy)
    report = await auto_cycle(env, cache, report_with()).run_once(DECISION)
    assert report.signal is not None and report.signal.active is not None
    assert report.signal.active.decision is Decision.BUY
    assert report.execution_status == "executed" and report.paused is None
    assert env.fake.position > 0 and env.previews == []  # никто не подтверждал
    assert any("BUY" in m for m in env.notifier.messages)  # оператор уведомлён


async def test_unexplained_difference_pauses_before_entry(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    env.engine.strategy = auto_strategy(env.engine.strategy)
    report = await auto_cycle(env, cache, report_with(WARNING)).run_once(DECISION)
    assert report.paused is not None and "чужие исполнения" in report.paused
    assert await blocked(env)
    assert report.execution_status == "blocked"  # guard: входы запрещены паузой
    assert env.order_actions() == []
    assert any("автопауза" in m for m in env.notifier.messages)

    # следующий проход с тем же расхождением не шлёт повторное уведомление
    sent = len(env.notifier.messages)
    await auto_cycle(env, cache, report_with(WARNING)).run_once(DECISION)
    assert not any("автопауза" in m for m in env.notifier.messages[sent:])


async def test_manual_mode_does_not_autopause(env: Env) -> None:
    cache = await prepare(env)
    cycle = auto_cycle(env, cache, report_with(WARNING))
    cycle.auto = False
    cycle.confirm = env.yes
    report = await cycle.run_once(DECISION)
    assert report.paused is None and not await blocked(env)


async def test_execution_error_pauses(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    env.engine.strategy = auto_strategy(env.engine.strategy)

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("биржа вернула нечто странное")

    monkeypatch.setattr(env.engine, "open_position", broken)
    report = await auto_cycle(env, cache, report_with()).run_once(DECISION)
    assert report.execution_status == "error"
    assert report.paused is not None and "ошибка исполнения" in report.paused
    assert await blocked(env)


async def test_trade_review_finding_pauses(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    cache = await prepare(env)
    env.engine.strategy = auto_strategy(env.engine.strategy)
    cycle = auto_cycle(env, cache, report_with())
    bad = review.RoundTrip(
        side="long",
        opened_at=env.clock() - timedelta(minutes=30),
        closed_at=env.clock() + timedelta(seconds=1),
        size=Decimal("0.0002"),
        entry_price=Decimal("65000"),
        exit_price=Decimal("64000"),
        fees=Decimal("0.01"),
        funding=Decimal(0),
        realized_pnl=Decimal("-0.2"),
        exit_kind="stop",
        bot_entry=True,
        checks=["защитный стоп не найден"],
    )

    async def fake_trips(*args: object, **kwargs: object) -> list[review.RoundTrip]:
        return [bad]

    monkeypatch.setattr(trader_module, "round_trips", fake_trips)
    report = await cycle.run_once(DECISION)
    assert report.paused is not None and "стоп не найден" in report.paused
    assert await blocked(env)
