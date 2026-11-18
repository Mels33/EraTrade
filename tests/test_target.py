"""Варианты метки: фиксированный порог, порог от волатильности, triple barrier."""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.features.target import Label, label_barrier, label_fixed, label_vol


@dataclass(frozen=True)
class Bar:
    high: Decimal
    low: Decimal
    close: Decimal


def bars(*hlc: tuple[str, str, str]) -> list[Bar]:
    return [Bar(Decimal(h), Decimal(lo), Decimal(c)) for h, lo, c in hlc]


ENTRY = ("100", "100", "100")


def test_fixed_uses_close_after_horizon() -> None:
    series = bars(ENTRY, ("101", "99", "100.1"), ("102", "99", "100.5"))
    out = label_fixed(series, 0, 2, Decimal("39.5"))
    assert out.label is Label.UP and out.long_return == pytest.approx(0.005)
    assert out.short_return == pytest.approx(-0.005)


def test_vol_threshold_scales_with_past_volatility() -> None:
    series = bars(
        ENTRY,
        ("101", "99", "100"),
        ("101", "99", "100"),
        ("101", "99", "100"),
        ("101", "99", "100.6"),
    )
    calm = label_vol(series, 0, 4, sigma_1h=0.001, k=1.0, floor_bps=Decimal("19.5"))
    wild = label_vol(series, 0, 4, sigma_1h=0.01, k=1.0, floor_bps=Decimal("19.5"))
    assert calm.label is Label.UP  # порог max(0.195%, 0.2%) < 0.6%
    assert wild.label is Label.FLAT  # порог 2% > 0.6%
    floor = label_vol(series, 0, 4, sigma_1h=0.0, k=1.0, floor_bps=Decimal("100"))
    assert floor.label is Label.FLAT  # нижняя граница порога — издержки + запас


def test_barrier_first_touch_wins() -> None:
    series = bars(ENTRY, ("100.4", "99.8", "100.2"), ("101.2", "100", "101"), ("101", "98", "98"))
    out = label_barrier(series, 0, 3, distance=0.01)
    assert out.label is Label.UP  # +1% во второй свече раньше −1% в третьей
    assert out.long_return == pytest.approx(0.01) and out.short_return == pytest.approx(-0.01)

    down = label_barrier(bars(ENTRY, ("100.5", "98.9", "99")), 0, 1, distance=0.01)
    assert down.label is Label.DOWN and down.short_return == pytest.approx(0.01)


def test_barrier_both_in_one_candle_is_conservative() -> None:
    out = label_barrier(bars(ENTRY, ("101.5", "98.5", "100")), 0, 1, distance=0.01)
    assert out.label is Label.FLAT
    assert out.long_return == pytest.approx(-0.01) and out.short_return == pytest.approx(-0.01)


def test_barrier_none_hit_exits_at_close() -> None:
    out = label_barrier(
        bars(ENTRY, ("100.5", "99.6", "100.3"), ("100.6", "99.7", "100.4")), 0, 2, distance=0.01
    )
    assert out.label is Label.FLAT and out.long_return == pytest.approx(0.004)


def test_barrier_ignores_candles_beyond_horizon() -> None:
    series = bars(ENTRY, ("100.2", "99.9", "100"), ("105", "95", "100"))
    assert label_barrier(series, 0, 1, distance=0.01).label is Label.FLAT
