"""Ловля проколов с усреднением: симулятор цикла и поиск монет."""

import io
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest

from app.research.averaging import COSTS, AvgParams, run
from app.research.wick_scan import _parse_zip, binance_symbol, months_back

P = AvgParams(step=0.01, levels=3, tp=0.005, stop=True)
D = Decimal


def path(*hlc: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    a = np.array(hlc, dtype=float)
    return a[:, 0], a[:, 1], a[:, 2]


def test_dip_bought_and_sold_on_rebound() -> None:
    # лимит 99 (1% ниже 100): прокол до 98.5 — покупка; цель 99 × 1.005 = 99.495
    h, lo, c = path((100, 100, 100), (100, 98.5, 99), (99.6, 99.0, 99.5))
    r = run(h, lo, c, P)
    assert r.cycles == 1 and r.wins == 1 and r.max_levels_used == 1
    assert r.total == pytest.approx((99.495 / 99 - 1 - 2 * COSTS.maker_fee) / 3)


def test_touch_is_not_a_fill_and_target_not_counted_in_fill_minute() -> None:
    h, lo, c = path((100, 100, 100), (100, 99.0, 99.5), (100, 99.5, 99.8))
    assert run(h, lo, c, P).cycles == 0  # low == лимит — не исполнено
    h, lo, c = path(
        (100, 100, 100), (100.0, 98.9, 99.0)
    )  # в минуте исполнения цель 99.495 не считается
    r = run(h, lo, c, P)
    assert r.cycles == 0 and r.max_levels_used == 1


def test_averaging_down_and_stop_after_last_level() -> None:
    # 99 → 98.01 → 97.03 (три уровня), затем ещё −1% от последнего — стоп
    h, lo, c = path(
        (100, 100, 100),
        (100, 98.9, 99),
        (99, 97.9, 98),
        (98, 96.9, 97),
        (97, 95.0, 95.5),
    )
    r = run(h, lo, c, P)
    assert r.max_levels_used == 3 and r.cycles == 1 and r.wins == 0
    assert r.worst_cycle < -0.02  # средняя ≈ 98.01, стоп ≈ 96.06 → около −2% капитала с издержками


def test_short_is_mirror() -> None:
    h, lo, c = path((100, 100, 100), (101.5, 100, 101), (101, 100.4, 100.5))
    r = run(h, lo, c, AvgParams(step=0.01, levels=3, tp=0.005, stop=True, side="short"))
    assert r.cycles == 1 and r.wins == 1


def test_binance_symbols_and_months() -> None:
    assert binance_symbol("BTC") == "BTCUSDT"
    assert binance_symbol("kPEPE") == "1000PEPEUSDT"
    assert binance_symbol("FARTCOIN") == "FARTCOINUSDT"
    assert months_back(3, datetime(2026, 11, 19, tzinfo=UTC)) == ["2026-08", "2026-09", "2026-10"]


@pytest.mark.parametrize("header", [True, False])
def test_parse_klines_zip_with_or_without_header(tmp_path: Path, header: bool) -> None:
    rows = "1790812800000,1.0,1.2,0.9,1.1,5,1790812859999,5.5,3,2,2.2,0\n"
    text = (
        "open_time,open,high,low,close,volume,close_time,qv,count,tbv,tbqv,ignore\n"
        if header
        else ""
    ) + rows
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("X-1m-2026-10.csv", text)
    p = tmp_path / "x.zip"
    p.write_bytes(buf.getvalue())
    df = _parse_zip(p)
    assert len(df) == 1 and df["high"].iloc[0] == 1.2 and df["close"].iloc[0] == 1.1


def test_wick_plan_sizes_and_level_reconstruction() -> None:
    from app.wick.strategy import CoinPlan, levels_filled

    plan = CoinPlan("X", D("0.012"), D("0.01"), 3, D(12), D("1.2"))
    assert [plan.level_usd(k) for k in range(3)] == [D(12), D("14.4"), D("17.28")]
    assert levels_filled(plan, D(0)) == 0
    assert levels_filled(plan, D(12)) == 1
    assert levels_filled(plan, D("26.5")) == 2
    assert levels_filled(plan, D("43.6")) == 3


def test_growing_sizes_weight_the_average() -> None:
    # покупки 99 и 98.01: веса 1 : 1.2 → средняя 98.46, цель +0.3% = 98.755;
    # при равных весах средняя 98.505, цель 98.80 — максимум 98.78 её не достаёт
    h, lo, c = path(
        (100, 100, 100), (100, 98.9, 99), (99, 97.9, 98.2), (98.6, 98.2, 98.5), (98.78, 98.4, 98.7)
    )
    grow = run(h, lo, c, AvgParams(step=0.01, levels=3, tp=0.003, stop=True, size_mult=1.2))
    equal = run(h, lo, c, AvgParams(step=0.01, levels=3, tp=0.003, stop=True))
    assert grow.cycles == 1 and grow.wins == 1
    assert equal.cycles == 0
