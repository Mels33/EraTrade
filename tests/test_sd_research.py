"""Исследование «спрос и предложение» на часовых свечах: агрегация, сигналы, исполнение."""

import numpy as np
import pandas as pd
import pytest

from app.research.execution_model import Costs
from app.research.hourly import DEEP_COLUMNS, to_hourly
from app.research.hourly_sim import Bars, Signal, non_overlapping, simulate
from app.research.sd_signals import (
    ImbalanceParams,
    WallParams,
    ZoneParams,
    delta_ratio,
    imbalance_signals,
    wall_signals,
    zone_signals,
)

C = Costs()


def minute_bars(n_hours: int = 2) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=60 * n_hours, freq="1min", tz="UTC")
    price = 100 + np.arange(len(idx)) * 0.01
    df = pd.DataFrame(
        {
            "minute": idx,
            "open": price,
            "high": price + 0.05,
            "low": price - 0.05,
            "close": price,
            "vwap": price,
            "buy_notional": 10.0,
            "sell_notional": 5.0,
            "depth_ok": False,  # ближней глубины нет, но ±1..5% есть
        }
    )
    for c in DEEP_COLUMNS:
        df[c] = 1000.0
    return df


def test_hourly_aggregation() -> None:
    h = to_hourly(minute_bars())
    assert len(h) == 2
    first = h.iloc[0]
    assert first["open"] == 100 and first["close"] == pytest.approx(100.59)
    assert first["high"] == pytest.approx(100.64) and first["low"] == pytest.approx(99.95)
    assert first["buy"] == 600 and first["sell"] == 300 and first["volume"] == 900
    assert bool(first["depth_ok"])  # глубина ±1..5% берётся и без ближней


def bars(*ohlc: tuple[float, float, float, float]) -> Bars:
    a = np.array(ohlc, dtype=float)
    return Bars(a[:, 0], a[:, 1], a[:, 2], a[:, 3])


def test_limit_fills_only_through_and_target_not_in_fill_bar() -> None:
    b = bars(
        (100, 100, 100, 100),
        (100, 100.5, 99.0, 99.5),  # low == 99 — касание, не исполнено
        (99.5, 103, 98.9, 102),  # прошли сквозь 99 — исполнено; цель 102 в этом часе не считается
        (102, 102.5, 101, 102),  # цель 102 — сквозь
    )
    sig = Signal(0, "long", "limit", stop=98.0, target=102.0, max_hours=1, price=99.0)
    f = simulate(b, sig)
    assert f is not None and f.entry_i == 2 and f.exit_i == 3
    assert f.net == pytest.approx(102 / 99 - 1 - 2 * C.maker_fee)


def test_stop_wins_when_both_hit_and_gap_exits_at_open() -> None:
    b = bars((100, 100, 100, 100), (100, 103, 97, 100), (100, 100, 100, 100))
    sig = Signal(0, "long", "market", stop=98.0, target=102.0, max_hours=1)
    f = simulate(b, sig)
    assert f is not None and f.exit_price == 98.0
    gap = bars((100, 100, 100, 100), (100, 100, 99, 99), (96, 97, 95, 96), (96, 96, 96, 96))
    f = simulate(gap, Signal(0, "long", "market", stop=98.0, target=None, max_hours=2))
    assert f is not None and f.exit_price == 96.0  # хуже стопа


def test_non_overlapping_keeps_one_position() -> None:
    b = bars(*[(100, 100.5, 99.5, 100)] * 30)
    fills = [
        simulate(b, Signal(i, "long", "market", stop=90, target=None, max_hours=5))
        for i in (0, 2, 7)
    ]
    taken = non_overlapping([f for f in fills if f is not None])
    assert [f.signal.i for f in taken] == [0, 7]


def synthetic_hourly(n: int = 400, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    # база (3 часа узко) и импульс вверх
    close[200:203] = close[199]
    close[203:206] = close[199] * np.array([1.02, 1.04, 1.06])
    idx = pd.date_range("2024-01-01", periods=n, freq="1h", tz="UTC")
    h = pd.DataFrame(
        {"open": np.r_[close[0], close[:-1]], "close": close},
        index=idx,
    )
    h["high"] = h[["open", "close"]].max(axis=1) * 1.001
    h["low"] = h[["open", "close"]].min(axis=1) * 0.999
    h["buy"] = rng.uniform(1, 2, n) * 1e6
    h["sell"] = rng.uniform(1, 2, n) * 1e6
    h["volume"] = h["buy"] + h["sell"]
    h["vwap"] = h["close"]
    for c in DEEP_COLUMNS:
        h[c] = rng.uniform(5, 10, n) * 1e7
    h["depth_ok"] = True
    return h


def test_zone_detected_after_base_and_impulse() -> None:
    h = synthetic_hourly()
    sigs = zone_signals(h, ZoneParams(base=2, k=2.0))
    longs = [s for s in sigs if s.side == "long" and 200 <= s.i <= 206]
    assert longs, "зона спроса после базы и импульса вверх"
    s = longs[0]
    assert s.entry == "limit" and s.price is not None and s.stop < s.price
    assert s.target is not None and s.target > s.price


@pytest.mark.parametrize(
    ("fn", "params"),
    [
        (zone_signals, ZoneParams()),
        (wall_signals, WallParams(band="1", thr=1.0)),
        (imbalance_signals, ImbalanceParams(days=1, thr=1.0)),
    ],
)
def test_no_lookahead(fn, params) -> None:  # type: ignore[no-untyped-def]
    h = synthetic_hourly(n=1500)
    cut = 1200
    full = [s for s in fn(h, params) if s.i < cut - 10]
    part = [s for s in fn(h.iloc[:cut], params) if s.i < cut - 10]
    assert full == part  # сигналы прошлого не зависят от будущих часов


def test_delta_ratio_sign() -> None:
    h = synthetic_hourly(n=300)
    h["buy"], h["sell"], h["volume"] = 3e6, 1e6, 4e6
    assert delta_ratio(h, 1).dropna().iloc[-1] == pytest.approx(0.5)
