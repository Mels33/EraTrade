"""Признаки v2 (30m): детерминизм, отсутствие заглядывания, поток сделок, отказ при ошибках."""

import dataclasses
import math
from datetime import timedelta
from decimal import Decimal

import pytest

from app.domain.market import Candle
from app.features.pipeline import FeatureError
from app.features.pipeline_v2 import (
    FEATURE_NAMES,
    LOOKBACK,
    STEP,
    FlowBar,
    binance_buy_ratio,
    compute_v2,
)
from tests.factories import T0, candle_series


def bars_30m(n: int, seed: int = 61, buy: float = 0.5) -> list[FlowBar]:
    hourly = candle_series(n, seed=seed)
    out = []
    for i, c in enumerate(hourly):
        open_time = T0 + i * STEP
        candle = dataclasses.replace(
            c,
            interval="30m",
            open_time=open_time,
            close_time=open_time + STEP,
            received_at=open_time + STEP,
            raw={"taker_buy": str(c.volume * Decimal(str(buy)))},
        )
        out.append(FlowBar(candle, buy + 0.1 * math.sin(i)))
    return out


def test_shape_and_known_values() -> None:
    bars = bars_30m(LOOKBACK + 10)
    v = compute_v2(bars)
    assert v.names == FEATURE_NAMES and len(v.values) == len(FEATURE_NAMES)
    assert v.decision_time == bars[-1].candle.close_time
    f = v.as_dict()
    c = [float(b.candle.close) for b in bars]
    assert f["ret_1"] == math.log(c[-1] / c[-2])
    assert f["buy_ratio_1"] == pytest.approx(bars[-1].buy_ratio - 0.5)
    assert -1 <= f["delta_4"] <= 1


def test_deterministic_and_fixed_window() -> None:
    bars = bars_30m(LOOKBACK + 50)
    assert compute_v2(bars) == compute_v2(list(bars)) == compute_v2(bars[-LOOKBACK:])


def test_no_lookahead() -> None:
    bars = bars_30m(LOOKBACK + 50)
    cut = LOOKBACK + 20
    before = compute_v2(bars[:cut])
    tampered = bars[:cut] + [dataclasses.replace(b, buy_ratio=1.0) for b in bars[cut:]]
    assert compute_v2(tampered[:cut]) == before


def test_flow_changes_flow_features_only() -> None:
    bars = bars_30m(LOOKBACK + 5)
    buyers = [*bars[:-1], dataclasses.replace(bars[-1], buy_ratio=0.95)]
    a, b = compute_v2(bars).as_dict(), compute_v2(buyers).as_dict()
    assert b["buy_ratio_1"] > a["buy_ratio_1"] and b["delta_4"] > a["delta_4"]
    assert a["ret_1"] == b["ret_1"] and a["rv_48"] == b["rv_48"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda bs: bs[: LOOKBACK - 1], "закрытых свечей"),
        (lambda bs: [*bs[:-1], dataclasses.replace(bs[-1], buy_ratio=math.nan)], "потока"),
        (lambda bs: bs[:-10] + bs[-9:], "пропуск"),
    ],
)
def test_bad_input_raises(mutate, message: str) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(FeatureError, match=message):
        compute_v2(mutate(bars_30m(LOOKBACK + 5)))


def test_binance_buy_ratio() -> None:
    c: Candle = bars_30m(1, buy=0.6)[0].candle
    assert binance_buy_ratio(c) == pytest.approx(0.6)
    assert math.isnan(binance_buy_ratio(dataclasses.replace(c, raw=None)))
    assert STEP == timedelta(minutes=30)
