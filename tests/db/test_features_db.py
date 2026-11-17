"""Критерий готовности этапа 10: вектор из БД «как в реальном времени» совпадает со строкой
обучающего набора, построенного по всей истории, — бит в бит, вместе с хешем входа."""

from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories import market as market_repo
from app.domain.market import FundingPoint
from app.features.dataset import TargetParams, build_dataset
from app.features.loader import features_at, load_history
from app.features.pipeline import LOOKBACK_CANDLES, FeatureError, features_for
from tests.factories import HOUR, T0, candle_series, funding_series, market_meta

N = LOOKBACK_CANDLES + 60
TARGET = TargetParams(horizon_candles=4, threshold_bps=Decimal("39.5"), cost_bps=Decimal("14.5"))


async def seed(session: AsyncSession) -> int:
    market_id = await market_repo.upsert_market(session, market_meta())
    await market_repo.upsert_candles(session, market_id, candle_series(N))
    await market_repo.upsert_funding_history(
        session, market_id, funding_series(T0 - 30 * HOUR, T0 + N * HOUR)
    )
    return market_id


async def test_funding_upsert_is_idempotent(session: AsyncSession) -> None:
    market_id = await market_repo.upsert_market(session, market_meta())
    points = funding_series(T0, T0 + 5 * HOUR)
    assert await market_repo.upsert_funding_history(session, market_id, points) == 5
    assert await market_repo.upsert_funding_history(session, market_id, points) == 0
    changed = FundingPoint(points[0].time, Decimal("0.0001"), points[0].premium)
    assert await market_repo.upsert_funding_history(session, market_id, [changed]) == 1
    stored = await market_repo.get_funding_history(session, market_id)
    assert stored[0].funding_rate == Decimal("0.0001") and len(stored) == 5
    assert await market_repo.get_last_funding_time(session, market_id) == points[-1].time


async def test_live_vector_equals_dataset_row(session: AsyncSession) -> None:
    market_id = await seed(session)
    candles, funding = await load_history(session, market_id, "1h")
    assert len(candles) == N
    dataset = build_dataset(candles, funding, TARGET)

    for i in (LOOKBACK_CANDLES - 1, LOOKBACK_CANDLES + 20, N - 1 - TARGET.horizon_candles):
        decision_time = candles[i].close_time
        inp, live = await features_at(session, market_id, "1h", HOUR, decision_time)
        row = next(r for r in dataset.rows if r.decision_time == decision_time)
        assert live.values == row.features
        # тот же вход из памяти (по всей истории) даёт тот же хеш
        known = [f for f in funding if f.time < decision_time]
        assert features_for(candles[: i + 1], known)[0].hash() == inp.hash()


async def test_latest_and_missing(session: AsyncSession) -> None:
    market_id = await seed(session)
    _, latest = await features_at(session, market_id, "1h", HOUR)
    assert latest.decision_time == T0 + N * HOUR
    with pytest.raises(FeatureError, match="нет закрытой свечи"):
        await features_at(session, market_id, "1h", HOUR, T0 + (N + 5) * HOUR)
    with pytest.raises(FeatureError, match="закрытых свечей"):
        await features_at(session, market_id, "1h", HOUR, T0 + 10 * HOUR)
