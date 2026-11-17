"""Mainnet shadow (этап 13): сигналы с рыночными условиями, без единого ордера, и отчёт."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.config.settings import Network, TradingMode
from app.db.models import ModelVersionRecord, SignalRecord
from app.db.repositories import control
from app.domain.account import BestBidAsk, Side
from app.signals.policy import Decision, PositionContext
from app.signals.report import shadow_report
from app.workers.shadow import ShadowConfig, ShadowObserver, executable_price, slippage_bps
from tests.db.test_trading_cycle import (
    DECISION,
    DOWN,
    PARAMS,
    UP,
    StubCache,
    StubPredictor,
    make_cycle,
    prepare,
)
from tests.db.trading_env import ACCOUNT, Env
from tests.factories import HOUR

BOOK = BestBidAsk(bid=Decimal("64999"), ask=Decimal("65001"), time=DECISION)


@pytest.mark.parametrize(
    ("decision", "side", "price", "exec_side"),
    [
        (Decision.BUY, None, Decimal("65001"), Side.BUY),
        (Decision.SELL, None, Decimal("64999"), Side.SELL),
        (Decision.EXIT, Side.BUY, Decimal("64999"), Side.SELL),
        (Decision.EXIT, Side.SELL, Decimal("65001"), Side.BUY),
        (Decision.HOLD, None, None, None),
    ],
)
def test_executable_price(
    decision: Decision, side: Side | None, price: Decimal | None, exec_side: Side | None
) -> None:
    assert executable_price(decision, PositionContext(side=side), BOOK) == (price, exec_side)


def test_slippage_sign() -> None:
    assert slippage_bps(Decimal("65065"), Decimal("65000"), Side.BUY) == pytest.approx(10.0)
    assert slippage_bps(Decimal("64935"), Decimal("65000"), Side.SELL) == pytest.approx(10.0)
    assert slippage_bps(Decimal("64990"), Decimal("65000"), Side.BUY) < 0  # цена лучше


def make_observer(env: Env, cache: StubCache) -> ShadowObserver:
    async def no_sleep(_: float) -> None:
        env.clock.now += timedelta(seconds=2)

    return ShadowObserver(
        env.engine.gateway,
        env.sm,
        env.engine.strategy,
        cache,
        network=Network.TESTNET,
        market_id=env.engine.market_id,
        coin="BTC",
        user=ACCOUNT,
        params=PARAMS,
        interval="1h",
        step=HOUR,
        clock=env.clock,
        config=ShadowConfig(candle_wait=4, poll_interval=2),
        sleep=no_sleep,
    )


async def signals(env: Env) -> list[SignalRecord]:
    async with env.sm() as db:
        stmt = select(SignalRecord).order_by(SignalRecord.decision_time, SignalRecord.id)
        return list((await db.execute(stmt)).scalars())


async def test_shadow_records_conditions_and_never_trades(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    report = await make_observer(env, cache).run_once(DECISION)

    assert report.signal is not None and report.signal.active is not None
    assert report.signal.active.decision is Decision.BUY
    assert env.fake.exchange_actions == []  # shadow ничего не подписывает
    row = (await signals(env))[0]
    assert row.market_bid == env.fake.bid and row.market_ask == env.fake.ask
    assert row.executable_price == env.fake.ask
    assert row.reference_price == Decimal("65000")  # close свечи 11:00–12:00 в env
    assert row.entry_slippage_bps == pytest.approx(float(Decimal("0.5") / 65000 * 10_000))
    fake_spread = (env.fake.ask - env.fake.bid) / ((env.fake.ask + env.fake.bid) / 2) * 10_000
    assert row.spread_bps == pytest.approx(float(fake_spread))
    assert row.risk_reasons == []  # режим TESTNET разрешает входы, лимиты не тронуты
    assert row.execution_status is None  # исполнять — не дело shadow
    async with env.sm() as db:
        beats = {h.component: h for h in await control.get_heartbeats(db)}
    assert beats["shadow"].details["decisions"] == {"active": "BUY"}


async def test_risk_refusal_reasons_recorded(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(DOWN)
    async with env.sm.begin() as db:
        await control.block_entries(
            db, Network.TESTNET, actor=control.SYSTEM_STARTUP, reason="risk: дневной лимит"
        )
    env.fake.bid, env.fake.ask = Decimal("64900"), Decimal("65100")  # спред ~31 bp
    await env.set_mode(TradingMode.DISABLED)
    await make_observer(env, cache).run_once(DECISION)
    reasons = " | ".join((await signals(env))[0].risk_reasons or [])
    assert "режим DISABLED" in reasons
    assert "входы запрещены" in reasons
    assert "спред" in reasons


async def test_shadow_reuses_signal_already_made_by_trader(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    await make_cycle(env, cache).run_once(DECISION)  # trader исполнил сигнал
    executed = (await signals(env))[0]
    assert executed.execution_status == "executed"

    report = await make_observer(env, cache).run_once(DECISION)
    assert report.signal is not None and all(d.replay for d in report.signal.decisions)
    rows = await signals(env)
    assert len(rows) == 1 and rows[0].market_ask is not None
    assert rows[0].execution_status == "executed"  # статус исполнения не тронут


async def test_shadow_report(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    observer = make_observer(env, cache)
    await observer.run_once(DECISION)
    async with env.sm.begin() as db:
        await db.execute(
            update(ModelVersionRecord).values(
                artifact={
                    "target": {"horizon_candles": 4},
                    "train_class_distribution": {"DOWN": 0.25, "FLAT": 0.5, "UP": 0.25},
                    "feature_names": ["ret_1", "rv_24"],
                    "feature_stats": {
                        "ret_1": {"mean": 0.0, "std": 0.001, "p01": -0.01, "p50": 0, "p99": 0.01},
                        "rv_24": {"mean": 1.0, "std": 0.01, "p01": 0.9, "p50": 1, "p99": 1.1},
                    },
                    "feature_bounds": {"ret_1": [-1, 1], "rv_24": [0.5, 1.5]},
                }
            )
        )
        await db.execute(
            update(SignalRecord).values(outcome_return=0.01, outcome_label="UP")
        )  # как будто горизонт наступил и цена выросла на 1%

    async with env.sm() as db:
        report = await shadow_report(
            db,
            env.engine.market_id,
            since=DECISION - timedelta(days=1),
            now=DECISION + timedelta(minutes=1),
            step=HOUR,
            cost_bps=14.5,
        )
    (model,) = report["models"]
    assert model["signals"] == 1 and model["coverage"] == 1.0
    assert model["decisions"] == {"BUY": 1}
    assert model["calibration"]["resolved"] == 1 and not model["calibration"]["enough"]
    assert model["calibration"]["log_loss"] < model["calibration"]["baseline_log_loss"]
    assert model["entries"]["precision"] == 1.0
    assert model["entries"]["mean_net_return"] == pytest.approx(0.01 - 0.00145)
    assert model["latency_s"]["p50"] is not None
    drift = {d["feature"]: d for d in model["drift"]}
    assert abs(drift["rv_24"]["shift_sigma"]) > 1  # rv_24 в живых данных ~0.006, train — 1.0
    assert drift["rv_24"]["out_of_bounds"] == 1.0
