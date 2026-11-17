"""Торговый цикл (этап 12) на симуляторе биржи: сигнал → исполнение, без повторов при сбоях."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config.settings import TradingMode
from app.db.models import ModelVersionRecord, SignalRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.execution.engine import Confirm
from app.features.pipeline import FeatureVector
from app.models.artifact import ArtifactError
from app.models.predictor import Prediction
from app.reconciliation.reconciler import (
    Difference,
    DiffKind,
    ReconciliationReport,
    Severity,
)
from app.signals.policy import Decision, PolicyParams
from app.signals.service import PredictorCache
from app.workers.trader import CycleConfig, TradingCycle
from tests.db.trading_env import NOW, Env
from tests.factories import HOUR, candle_series, funding_series

DECISION = NOW.replace(minute=0, second=0)  # 12:00 — конец последней закрытой свечи в env
VERSION = "cb-test-active"
OPERATOR = Actor(ActorKind.OPERATOR, "tester")
PARAMS = PolicyParams(
    min_probability=0.45,
    min_margin=0.15,
    min_edge=0.00195,
    allow_long=True,
    allow_short=True,
    time_exit_candles=8,
    reentry_cooldown_candles=1,
    step=HOUR,
)

UP = {"DOWN": 0.10, "FLAT": 0.30, "UP": 0.60}
DOWN = {"DOWN": 0.60, "FLAT": 0.30, "UP": 0.10}
NEUTRAL = {"DOWN": 0.30, "FLAT": 0.40, "UP": 0.30}


class StubPredictor:
    def __init__(self, probs: dict[str, float] | None, fail: bool = False) -> None:
        self.probs, self.fail = probs, fail

    def predict(self, vector: FeatureVector) -> Prediction:
        if self.fail or self.probs is None:
            raise ArtifactError("модель недоступна")
        edge = (self.probs["UP"] - self.probs["DOWN"]) * 0.01
        return Prediction(VERSION, dict(self.probs), (0.0, 0.0, 0.0), edge)


class StubCache(PredictorCache):
    def __init__(self) -> None:
        super().__init__(models_root=Path("/nonexistent"))
        self.predictor = StubPredictor(NEUTRAL)

    def get(self, record: ModelVersionRecord) -> StubPredictor:  # type: ignore[override]
        return self.predictor


def ok_report(critical: bool = False) -> ReconciliationReport:
    diffs = (
        (
            Difference(
                DiffKind.POSITION_MISMATCH,
                Severity.CRITICAL,
                "позиция не совпадает",
                repaired=False,
            ),
        )
        if critical
        else ()
    )
    return ReconciliationReport(
        run_id=1,
        status="critical" if critical else "ok",
        differences=diffs,
        exchange_position=Decimal(0),
        expected_position=Decimal(0),
        entries_blocked=critical,
    )


async def prepare(env: Env) -> StubCache:
    """История для признаков (169+ свечей до 12:00 и funding) и активная модель в реестре."""
    start = DECISION - 220 * HOUR
    series = candle_series(200, start=start, seed=9)  # до 08:00 позавчера; дальше — свечи env
    async with env.sm.begin() as db:
        await market_repo.upsert_candles(db, env.engine.market_id, series)
        await market_repo.upsert_funding_history(
            db, env.engine.market_id, funding_series(start - 30 * HOUR, DECISION, seed=4)
        )
        db.add(
            ModelVersionRecord(
                version=VERSION,
                status="active",
                feature_set_version="v1",
                model_sha256="0" * 64,
                artifact_path=VERSION,
                artifact={"target": {"horizon_candles": 4}},
            )
        )
    return StubCache()


def make_cycle(
    env: Env, cache: StubCache, *, critical: bool = False, confirm: Confirm | None = None
) -> TradingCycle:
    async def reconcile() -> ReconciliationReport:
        return ok_report(critical)

    async def no_sleep(_: float) -> None:
        env.clock.now += timedelta(seconds=2)

    return TradingCycle(
        env.engine,
        cache,
        params=PARAMS,
        interval="1h",
        step=HOUR,
        max_delay=timedelta(seconds=90),
        confirm=confirm or env.yes,
        reconcile=reconcile,
        config=CycleConfig(candle_wait=4, poll_interval=2),
        sleep=no_sleep,
    )


async def signal_row(env: Env) -> SignalRecord:
    async with env.sm() as db:
        return (
            await db.execute(select(SignalRecord).where(SignalRecord.decision_time == DECISION))
        ).scalar_one()


async def test_buy_signal_opens_position_with_stop_once(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    cycle = make_cycle(env, cache)

    report = await cycle.run_once(DECISION)
    assert report.signal is not None and report.signal.feature_error is None
    assert report.signal.active is not None and report.signal.active.decision is Decision.BUY
    assert report.execution_status == "executed", report.execution_detail
    assert env.fake.position > 0
    assert len(env.order_actions()) == 2  # вход + защитный стоп
    row = await signal_row(env)
    assert row.execution_status == "executed" and row.execution_order_id is not None

    # повтор прохода по той же свече: сигнал уже исполнен — новых ордеров нет
    again = await cycle.run_once(DECISION)
    assert again.signal is not None and again.signal.active is not None
    assert again.signal.active.replay
    assert again.execution_status == "already_handled"
    assert len(env.order_actions()) == 2


async def test_crash_mid_execution_is_never_retried(env: Env) -> None:
    """Процесс упал после claim (статус executing): новый процесс не исполняет сигнал."""
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    first = make_cycle(env, cache)

    async def crash(*args: object, **kwargs: object) -> None:
        raise SystemExit("процесс убит посреди отправки")

    env.engine.open_position = crash  # type: ignore[method-assign]
    with pytest.raises(SystemExit):
        await first.run_once(DECISION)
    assert (await signal_row(env)).execution_status == "executing"

    del env.engine.open_position  # «новый процесс» с нормальным движком
    restarted = make_cycle(env, cache)
    report = await restarted.run_once(DECISION)
    assert report.execution_status == "already_handled"
    assert env.order_actions() == []


async def test_exit_signal_closes_without_reversal(env: Env) -> None:
    cache = await prepare(env)
    await env.engine.open_position(Side.BUY, Decimal("15"), confirm=env.yes)
    cache.predictor = StubPredictor(DOWN)
    report = await make_cycle(env, cache).run_once(DECISION)
    assert report.signal is not None and report.signal.active is not None
    assert report.signal.active.decision is Decision.EXIT
    assert report.execution_status == "executed"
    assert env.fake.position == 0  # закрыта, short не открыт


async def test_hold_sends_nothing(env: Env) -> None:
    cache = await prepare(env)
    report = await make_cycle(env, cache).run_once(DECISION)
    assert report.execution_status == "none"
    assert (await signal_row(env)).execution_status == "none"

    assert env.order_actions() == []


async def test_model_failure_gives_hold(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(None, fail=True)
    report = await make_cycle(env, cache).run_once(DECISION)
    assert report.signal is not None and report.signal.active is not None
    assert report.signal.active.decision is Decision.HOLD
    assert "модель" in report.signal.active.reason
    assert env.order_actions() == []


async def test_missing_candle_gives_hold(env: Env) -> None:
    """Коллектор не сохранил свечу 13:00 (устаревшие данные) — HOLD, ордеров нет."""
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    env.clock.now = DECISION + HOUR + timedelta(seconds=10)
    report = await make_cycle(env, cache).run_once(DECISION + HOUR)
    assert "не появилась" in " ".join(report.warnings)
    assert report.signal is not None and report.signal.feature_error is not None
    assert report.execution_status == "none"
    assert env.order_actions() == []


async def test_late_decision_is_not_executed(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    env.clock.now = DECISION + timedelta(minutes=5)
    report = await make_cycle(env, cache).run_once(DECISION)
    assert report.signal is not None and "опаздывает" in (report.signal.feature_error or "")
    assert report.execution_status == "none"
    assert env.order_actions() == []


async def test_critical_reconciliation_blocks_entry(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    report = await make_cycle(env, cache, critical=True).run_once(DECISION)
    assert report.execution_status == "skipped" and "сверки" in report.execution_detail
    assert env.order_actions() == []


async def test_paused_mode_blocks_entry_by_guard(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    await env.set_mode(TradingMode.DISABLED)
    report = await make_cycle(env, cache).run_once(DECISION)
    assert report.execution_status == "blocked", report.execution_detail
    assert env.order_actions() == []


async def test_operator_declines(env: Env) -> None:
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    report = await make_cycle(env, cache, confirm=env.no).run_once(DECISION)
    assert report.execution_status == "declined"
    assert env.order_actions() == []


async def test_unknown_send_outcome_is_not_retried(env: Env) -> None:
    """Соединение оборвалось после отправки: исход неизвестен — не повторяем вслепую."""
    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)
    env.fake.fail_next = "timeout_after"
    report = await make_cycle(env, cache).run_once(DECISION)
    assert report.execution_status in ("error", "executed"), report.execution_detail
    sent = len(env.order_actions())
    again = await make_cycle(env, cache).run_once(DECISION)
    assert again.execution_status == "already_handled"
    assert len(env.order_actions()) == sent


async def test_heartbeat_written(env: Env) -> None:
    cache = await prepare(env)
    await make_cycle(env, cache).run_once(DECISION)
    async with env.sm() as db:
        beats = {h.component: h for h in await control.get_heartbeats(db)}
    assert beats["trader"].details["last_execution"] == "none"


async def test_database_failure_before_send_sends_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PostgreSQL недоступен в момент claim: проход падает, на биржу ничего не уходит."""
    from app.db.repositories import models as models_repo

    cache = await prepare(env)
    cache.predictor = StubPredictor(UP)

    async def db_down(*args: object, **kwargs: object) -> bool:
        raise ConnectionRefusedError("PostgreSQL недоступен")

    monkeypatch.setattr(models_repo, "claim_execution", db_down)
    with pytest.raises(ConnectionRefusedError):
        await make_cycle(env, cache).run_once(DECISION)
    assert env.order_actions() == []
