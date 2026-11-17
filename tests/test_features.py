"""Feature pipeline v1: детерминизм, отсутствие заглядывания в будущее, отказ при плохих данных."""

import dataclasses
import math
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.market import Candle, FundingPoint
from app.features.pipeline import (
    FEATURE_NAMES,
    LOOKBACK_CANDLES,
    FeatureError,
    compute_features,
    features_for,
    prepare_input,
)
from tests.factories import HOUR, T0, candle_series, funding_series

N = LOOKBACK_CANDLES + 50


@pytest.fixture
def candles() -> list[Candle]:
    return candle_series(N)


@pytest.fixture
def funding() -> list[FundingPoint]:
    return funding_series(T0 - 30 * HOUR, T0 + (N + 10) * HOUR)


def test_vector_shape_and_known_values(candles: list[Candle], funding: list[FundingPoint]) -> None:
    inp, vector = features_for(candles, funding)
    assert vector.names == FEATURE_NAMES and len(vector.values) == len(FEATURE_NAMES)
    assert vector.decision_time == candles[-1].close_time
    f = vector.as_dict()
    c = [float(x.close) for x in candles]
    assert f["ret_1"] == math.log(c[-1] / c[-2])
    assert f["ret_24"] == math.log(c[-1] / c[-25])
    hour = vector.decision_time.hour
    assert f["hour_sin"] == pytest.approx(math.sin(2 * math.pi * hour / 24))
    # funding — последнее начисление строго до момента решения, в bp
    assert inp.funding[-1].time < vector.decision_time
    assert f["funding_last_bps"] == float(inp.funding[-1].funding_rate) * 1e4
    assert len(inp.candles) == LOOKBACK_CANDLES and len(inp.funding) == 24


def test_deterministic_vector_and_hash(candles: list[Candle], funding: list[FundingPoint]) -> None:
    inp1, v1 = features_for(candles, funding)
    inp2, v2 = features_for(list(candles), list(funding))
    assert v1 == v2 and inp1.hash() == inp2.hash()


def test_hash_is_canonical_but_sensitive(
    candles: list[Candle], funding: list[FundingPoint]
) -> None:
    inp, _ = features_for(candles, funding)
    last = candles[-1]
    same = dataclasses.replace(last, volume=Decimal(f"{last.volume}000"))  # 1.5 == 1.5000
    assert features_for([*candles[:-1], same], funding)[0].hash() == inp.hash()
    other = dataclasses.replace(last, volume=last.volume + Decimal("0.00001"))
    assert features_for([*candles[:-1], other], funding)[0].hash() != inp.hash()


def test_extra_history_does_not_change_vector(
    candles: list[Candle], funding: list[FundingPoint]
) -> None:
    """Окно фиксированное: более длинная история слева не влияет на вектор."""
    _, full = features_for(candles, funding)
    _, short = features_for(candles[-LOOKBACK_CANDLES:], funding)
    assert full == short


def test_no_lookahead(candles: list[Candle], funding: list[FundingPoint]) -> None:
    """Будущие свечи и funding после момента решения не влияют на вектор."""
    cut = N - 20
    _, before = features_for(candles[:cut], funding)
    future_funding = [f for f in funding if f.time >= candles[cut - 1].close_time]
    tampered_funding = [f for f in funding if f.time < candles[cut - 1].close_time] + [
        dataclasses.replace(f, funding_rate=Decimal("0.01")) for f in future_funding
    ]
    _, after = features_for(candles[:cut], tampered_funding)
    assert before == after


def test_funding_at_decision_boundary_is_not_used(
    candles: list[Candle], funding: list[FundingPoint]
) -> None:
    decision = candles[-1].close_time
    known = [f for f in funding if f.time < decision]
    exact = dataclasses.replace(known[-1], time=decision, funding_rate=Decimal("0.5"))
    inp, _ = features_for(candles, [*known, exact])
    assert exact not in inp.funding


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda cs: cs[: LOOKBACK_CANDLES - 1], "закрытых свечей"),
        (lambda cs: [*cs[:-1], dataclasses.replace(cs[-1], is_closed=False)], "не закрыта"),
        (lambda cs: cs[:-10] + cs[-9:], "пропуск свечей"),
        (lambda cs: [*cs[:-1], dataclasses.replace(cs[-1], trades_count=0)], "сделок"),
    ],
)
def test_bad_candles_raise(
    candles: list[Candle],
    funding: list[FundingPoint],
    mutate: Callable[[list[Candle]], list[Candle]],
    message: str,
) -> None:
    with pytest.raises(FeatureError, match=message):
        prepare_input(mutate(candles), funding)


def test_stale_or_sparse_funding_raises(candles: list[Candle], funding: list[FundingPoint]) -> None:
    decision = candles[-1].close_time
    known = [f for f in funding if f.time < decision]
    with pytest.raises(FeatureError, match="устарел"):
        prepare_input(candles, known[:-3])
    sparse = known[-40:-30] + known[-14:]  # 24 начисления, но растянуты на 40 ч
    with pytest.raises(FeatureError, match="пропуски"):
        prepare_input(candles, sparse)
    with pytest.raises(FeatureError, match="начислений funding"):
        prepare_input(candles, known[-10:])


def test_flat_candle_close_pos_and_finite_values(
    candles: list[Candle], funding: list[FundingPoint]
) -> None:
    last = candles[-1]
    p = last.open
    flat = dataclasses.replace(last, high=p, low=p, close=p)
    _, vector = features_for([*candles[:-1], flat], funding)
    assert vector.as_dict()["close_pos"] == 0.5
    assert all(math.isfinite(x) for x in vector.values)


def test_compute_features_uses_only_prepared_window(
    candles: list[Candle], funding: list[FundingPoint]
) -> None:
    inp = prepare_input(candles, funding, step=timedelta(hours=1))
    assert compute_features(inp) == features_for(candles, funding)[1]
