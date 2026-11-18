"""Эксперимент с целевой переменной: отбор сделок без наложения, подбор порога, смоук."""

from decimal import Decimal

import pytest

from app.features.target import Label, Outcome
from app.models.experiment import (
    DEFAULT_VARIANTS,
    ExpRow,
    Trade,
    choose_theta,
    run_experiment,
    select_trades,
    trade_stats,
)
from tests.factories import HOUR, T0, candle_series, funding_series

MEANS = {Label.DOWN: -0.01, Label.FLAT: 0.0, Label.UP: 0.01}
SHORT = {Label.DOWN: 0.01, Label.FLAT: 0.0, Label.UP: -0.01}


def rows(n: int) -> list[ExpRow]:
    return [ExpRow(T0 + i * HOUR, (), i, "hyperliquid", "validation") for i in range(n)]


def test_trades_do_not_overlap_and_respect_threshold() -> None:
    r = rows(10)
    outcomes = [Outcome(Label.UP, 0.01, -0.01)] * 10
    confident = [[0.1, 0.2, 0.7]] * 10
    trades = select_trades(
        r,
        outcomes,
        confident,
        theta=0.5,
        mean_long=MEANS,
        mean_short=SHORT,
        edge=0.00195,
        cost=0.00145,
        horizon=4,
    )
    assert [t.time for t in trades] == [T0, T0 + 4 * HOUR, T0 + 8 * HOUR]
    assert all(t.side == "long" and t.net == pytest.approx(0.01 - 0.00145) for t in trades)

    unsure = select_trades(
        r,
        outcomes,
        [[0.3, 0.35, 0.35]] * 10,
        theta=0.5,
        mean_long=MEANS,
        mean_short=SHORT,
        edge=0.00195,
        cost=0.00145,
        horizon=4,
    )
    assert unsure == []


def test_expected_return_gate_blocks_weak_edge() -> None:
    r = rows(3)
    outcomes = [Outcome(Label.UP, 0.01, -0.01)] * 3
    probs = [[0.25, 0.25, 0.5]] * 3  # E[r] = 0.25% > 0.195%
    assert select_trades(
        r,
        outcomes,
        probs,
        theta=0.45,
        mean_long=MEANS,
        mean_short=SHORT,
        edge=0.00195,
        cost=0.0,
        horizon=1,
    )
    weak = [[0.4, 0.1, 0.5]] * 3  # E[r] = 0.1% < 0.195%
    assert not select_trades(
        r,
        outcomes,
        weak,
        theta=0.45,
        mean_long=MEANS,
        mean_short=SHORT,
        edge=0.00195,
        cost=0.0,
        horizon=1,
    )


def test_choose_theta_and_stats() -> None:
    curve = [
        {"theta": 0.4, "trades": 40, "per_day": 2.0},
        {"theta": 0.5, "trades": 20, "per_day": 1.0},
        {"theta": 0.6, "trades": 0, "per_day": 0.0},
    ]
    assert choose_theta(curve, 1.0)["theta"] == 0.5  # type: ignore[index]
    assert choose_theta([{"theta": 0.6, "trades": 0, "per_day": 0.0}], 1.0) is None
    stats = trade_stats(
        [Trade(T0, "long", 0.01), Trade(T0, "long", -0.01), Trade(T0, "short", -0.02)], days=2
    )
    assert stats["trades"] == 3 and stats["per_day"] == 1.5
    assert stats["worst_losing_streak"] == 2 and stats["hit_rate"] == pytest.approx(1 / 3)


def test_run_experiment_smoke() -> None:
    candles = candle_series(900, seed=41)
    funding = funding_series(T0 - 30 * HOUR, T0 + 910 * HOUR, seed=42)
    aux_candles = candle_series(1200, start=T0 - 1100 * HOUR, seed=43)
    aux_funding = funding_series(T0 - 1140 * HOUR, T0 + 110 * HOUR, seed=44)
    report = run_experiment(
        (candles, funding),
        (aux_candles, aux_funding),
        list(DEFAULT_VARIANTS.values()),
        cost_bps=Decimal("14.5"),
        edge_bps=Decimal("19.5"),
        trades_per_day=1.0,
        catboost_params={"iterations": 30, "od_wait": 10},
    )
    assert {v["variant"] for v in report["variants"]} == set(DEFAULT_VARIANTS)
    assert report["rows_by_source"]["binance"] > 0
    for v in report["variants"]:
        assert set(v["segments"]) == {"validation", "test"}
        assert len(v["theta_curve_validation"]) > 10
        if v["kind"] == "vol":
            assert v["k"] > 0
