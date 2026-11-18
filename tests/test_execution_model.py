"""Модель исполнения для исследования: maker-вход только при проходе сквозь лимит,
консервативный порядок событий, издержки taker / maker."""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.research.execution_model import Costs, simulate

C = Costs()


@dataclass(frozen=True)
class Bar:
    high: Decimal
    low: Decimal
    close: Decimal


def bars(*hlc: tuple[float, float, float]) -> list[Bar]:
    return [Bar(Decimal(str(h)), Decimal(str(lo)), Decimal(str(c))) for h, lo, c in hlc]


ENTRY = (100.0, 100.0, 100.0)


def run(series: list[Bar], **kw: object) -> float | None:
    params = {"side": "long", "horizon": 3, "distance": 0.01, "take_profit": True, "model": "maker"}
    params.update(kw)
    return simulate(series, 0, **params)  # type: ignore[arg-type]


def test_maker_needs_price_through_limit() -> None:
    touch_only = bars(ENTRY, (100.5, 100.0, 100.3), (101, 100, 100.5), (101, 100, 100.5))
    assert run(touch_only) is None  # low == limit — не исполнено
    short_no = bars(ENTRY, (100.0, 99.5, 99.7), (100, 99, 99.5), (100, 99, 99.5))
    assert run(short_no, side="short") is None


def test_maker_take_profit_needs_trade_through_and_not_in_fill_candle() -> None:
    s = bars(ENTRY, (101.5, 99.9, 100.2), (101.0, 100.1, 100.8), (101.2, 100.5, 101.0))
    # в свече исполнения high 101.5 > TP, но не засчитано; затем 101.0 == TP — не сквозь; 101.2 > TP
    assert run(s) == pytest.approx(0.01 - 2 * C.maker_fee)


def test_stop_in_fill_candle_counts() -> None:
    s = bars(ENTRY, (100.2, 98.9, 99.0), (101, 99, 100), (101, 99, 100))
    assert run(s) == pytest.approx(-0.01 - C.maker_fee - C.taker_side)


def test_both_barriers_in_one_candle_is_stop() -> None:
    s = bars(ENTRY, (100.2, 99.9, 100.0), (101.5, 98.5, 100.0), (101, 99, 100))
    assert run(s) == pytest.approx(-0.01 - C.maker_fee - C.taker_side)


def test_time_exit_and_taker_costs() -> None:
    s = bars(ENTRY, (100.3, 99.8, 100.1), (100.4, 99.7, 100.2), (100.5, 99.6, 100.3))
    assert run(s) == pytest.approx(0.003 - C.maker_fee - C.taker_side)
    taker = run(s, model="taker")
    assert taker == pytest.approx(0.003 - 2 * C.taker_side)
    assert 2 * C.taker_side == pytest.approx(0.0014)  # ≈ 14.5 bp текущей спецификации


def test_short_mirror_and_no_take_profit_variant() -> None:
    s = bars(ENTRY, (100.1, 99.8, 99.9), (99.5, 98.8, 99.0), (99.5, 98.5, 98.7))
    assert run(s, side="short") == pytest.approx(0.01 - 2 * C.maker_fee)
    no_tp = run(s, side="short", take_profit=False)
    assert no_tp == pytest.approx(1 - 98.7 / 100 - C.maker_fee - C.taker_side)
