"""Реестр моделей (§15.4) и генерация сигнала на закрытую свечу (§15.1)."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLogRecord, FeatureSnapshotRecord, SignalRecord
from app.db.repositories import market as market_repo
from app.db.repositories import models as models_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.features.target import forward_return
from app.models.artifact import ModelArtifact
from app.signals.policy import Decision, PositionContext
from app.signals.service import (
    PredictorCache,
    fill_outcomes,
    generate_signal,
)
from tests.factories import HOUR, T0, market_meta
from tests.modeling import N_CANDLES, PARAMS, history, train_small

OPERATOR = Actor(ActorKind.OPERATOR, "test")
FLAT = PositionContext(side=None)


@pytest.fixture(scope="module")
def trained(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, ModelArtifact, ModelArtifact]:
    root = tmp_path_factory.mktemp("trained")
    first = train_small(root, iterations=60, minute=0)
    second = train_small(root, iterations=40, minute=1)
    return root / "models", first, second


async def register(session: AsyncSession, models_dir: Path, artifact: ModelArtifact) -> None:
    await models_repo.register_model(
        session,
        artifact,
        actor=OPERATOR,
        artifact_path=str(artifact.path.relative_to(models_dir)),
    )


async def audit_actions(session: AsyncSession) -> list[str]:
    stmt = (
        select(AuditLogRecord.action)
        .where(AuditLogRecord.action.like("model.%"), AuditLogRecord.occurred_at >= func.now())
        .order_by(AuditLogRecord.id)
    )
    return list((await session.execute(stmt)).scalars())


async def test_registry_lifecycle_and_rollback(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> None:
    models_dir, a, b = trained
    await register(session, models_dir, a)
    await register(session, models_dir, b)
    with pytest.raises(models_repo.RegistryError, match="уже зарегистрирована"):
        await register(session, models_dir, a)

    # без shadow активировать нельзя
    with pytest.raises(models_repo.RegistryError, match="после shadow"):
        await models_repo.activate(session, a.version, actor=OPERATOR, reason="сразу")

    await models_repo.set_shadow(session, a.version, actor=OPERATOR, reason="наблюдение")
    await models_repo.activate(session, a.version, actor=OPERATOR, reason="shadow ок")
    await models_repo.set_shadow(session, b.version, actor=OPERATOR, reason="новая")
    await models_repo.activate(session, b.version, actor=OPERATOR, reason="лучше")

    statuses = {r.version: r.status for r in await models_repo.list_models(session)}
    assert statuses == {a.version: "retired", b.version: "active"}

    # откат на прежнюю активную — без повторного shadow
    await models_repo.activate(session, a.version, actor=OPERATOR, reason="откат")
    statuses = {r.version: r.status for r in await models_repo.list_models(session)}
    assert statuses == {a.version: "active", b.version: "retired"}
    assert await audit_actions(session) == [
        "model.registered",
        "model.registered",
        "model.shadow",
        "model.activated",
        "model.shadow",
        "model.activated",
        "model.rollback",
    ]


async def test_shadow_replaces_previous_shadow(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> None:
    models_dir, a, b = trained
    await register(session, models_dir, a)
    await register(session, models_dir, b)
    await models_repo.set_shadow(session, a.version, actor=OPERATOR, reason="1")
    await models_repo.set_shadow(session, b.version, actor=OPERATOR, reason="2")
    statuses = {r.version: r.status for r in await models_repo.list_models(session)}
    assert statuses == {a.version: "candidate", b.version: "shadow"}


async def seeded(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> tuple[int, PredictorCache]:
    models_dir, a, b = trained
    market_id = await market_repo.upsert_market(session, market_meta())
    candles, funding = history()
    await market_repo.upsert_candles(session, market_id, candles)
    await market_repo.upsert_funding_history(session, market_id, funding)
    await register(session, models_dir, a)
    await register(session, models_dir, b)
    await models_repo.set_shadow(session, a.version, actor=OPERATOR, reason="t")
    await models_repo.activate(session, a.version, actor=OPERATOR, reason="t")
    await models_repo.set_shadow(session, b.version, actor=OPERATOR, reason="t")
    return market_id, PredictorCache(models_dir)


async def test_signal_for_active_and_shadow_is_idempotent(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> None:
    market_id, cache = await seeded(session, trained)
    kwargs = dict(
        market_id=market_id,
        interval="1h",
        step=HOUR,
        params=PARAMS,
        position=FLAT,
        predictors=cache,
    )
    first = await generate_signal(session, **kwargs)  # type: ignore[arg-type]
    assert first.decision_time == T0 + N_CANDLES * HOUR
    assert first.feature_error is None and first.input_hash
    assert [d.role for d in first.decisions] == ["active", "shadow"]
    for d in first.decisions:
        assert d.probabilities is not None and sum(d.probabilities.values()) == pytest.approx(1)
        assert d.decision in set(Decision) and not d.replay

    again = await generate_signal(session, **kwargs)  # type: ignore[arg-type]
    assert all(d.replay for d in again.decisions)
    assert [d.signal_id for d in again.decisions] == [d.signal_id for d in first.decisions]
    snapshots = (
        await session.execute(select(func.count()).select_from(FeatureSnapshotRecord))
    ).scalar_one()
    signals = (await session.execute(select(func.count()).select_from(SignalRecord))).scalar_one()
    assert (snapshots, signals) == (1, 2)


async def test_feature_error_gives_hold_but_time_exit_still_works(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> None:
    market_id, cache = await seeded(session, trained)
    too_early = T0 + 50 * HOUR  # меньше 169 свечей истории
    result = await generate_signal(
        session,
        market_id=market_id,
        interval="1h",
        step=HOUR,
        params=PARAMS,
        position=FLAT,
        predictors=cache,
        decision_time=too_early,
    )
    assert result.feature_error is not None
    assert {d.decision for d in result.decisions} == {Decision.HOLD}
    assert all(d.probabilities is None for d in result.decisions)

    old_long = PositionContext(side=Side.BUY, opened_at=too_early - 9 * HOUR)
    exit_result = await generate_signal(
        session,
        market_id=market_id,
        interval="1h",
        step=HOUR,
        params=PARAMS,
        position=old_long,
        predictors=cache,
        decision_time=too_early + HOUR,
    )
    assert {d.decision for d in exit_result.decisions} == {Decision.EXIT}


async def test_stale_decision_is_hold(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> None:
    market_id, cache = await seeded(session, trained)
    result = await generate_signal(
        session,
        market_id=market_id,
        interval="1h",
        step=HOUR,
        params=PARAMS,
        position=FLAT,
        predictors=cache,
        now=T0 + N_CANDLES * HOUR + timedelta(minutes=10),
        max_delay=timedelta(seconds=90),
    )
    assert result.feature_error is not None and "опаздывает" in result.feature_error
    assert {d.decision for d in result.decisions} == {Decision.HOLD}


async def test_outcomes_filled_after_horizon(
    session: AsyncSession, trained: tuple[Path, ModelArtifact, ModelArtifact]
) -> None:
    market_id, cache = await seeded(session, trained)
    decision_time = T0 + (N_CANDLES - 10) * HOUR
    await generate_signal(
        session,
        market_id=market_id,
        interval="1h",
        step=HOUR,
        params=PARAMS,
        position=FLAT,
        predictors=cache,
        decision_time=decision_time,
    )
    latest = await generate_signal(
        session,
        market_id=market_id,
        interval="1h",
        step=HOUR,
        params=PARAMS,
        position=FLAT,
        predictors=cache,
    )
    filled = await fill_outcomes(
        session,
        market_id=market_id,
        interval="1h",
        step=HOUR,
        threshold_bps=Decimal("39.5"),
        now=latest.decision_time,
    )
    assert filled == 2  # оба сигнала момента decision_time; у последних горизонт не наступил
    candles, _ = history()
    expected = forward_return(candles[N_CANDLES - 11].close, candles[N_CANDLES - 7].close)
    rows = (
        await session.execute(
            select(SignalRecord).where(SignalRecord.decision_time == decision_time)
        )
    ).scalars()
    for row in rows:
        assert row.outcome_return == expected and row.outcome_label in {"UP", "DOWN", "FLAT"}


async def test_position_context_from_fills(session: AsyncSession) -> None:
    from app.db.models import FillRecord
    from app.signals.service import position_context

    market_id = await market_repo.upsert_market(session, market_meta())

    def fill(tid: int, hours: float, side: str, size: str, start: str) -> FillRecord:
        return FillRecord(
            tid=tid,
            oid=tid,
            market_id=market_id,
            coin="BTC",
            side=side,
            price=Decimal("90000"),
            size=Decimal(size),
            fee=Decimal("0"),
            fee_token="USDC",
            closed_pnl=Decimal("0"),
            direction="",
            start_position=Decimal(start),
            hash=f"0x{tid}",
            time=T0 + timedelta(hours=hours),
            raw={},
        )

    session.add_all(
        [
            fill(1, 1, "buy", "0.0002", "0"),  # открыли long
            fill(2, 3, "sell", "0.0002", "0.0002"),  # закрыли
            fill(3, 5, "sell", "0.0001", "0"),  # открыли short
            fill(4, 5.01, "sell", "0.0001", "-0.0001"),  # добор того же ордера
        ]
    )
    await session.flush()
    ctx = await position_context(session, market_id, Side.SELL)
    assert ctx.opened_at == T0 + timedelta(hours=5)
    assert ctx.last_closed_at == T0 + timedelta(hours=3)
    flat = await position_context(session, market_id, None)
    assert flat.opened_at is None and flat.last_closed_at == T0 + timedelta(hours=3)
