"""Признаки v2 для 30-минутного таймфрейма: цена/волатильность/объём + поток сделок.

Как и v1 — чистая функция фиксированного окна из LOOKBACK закрытых свечей (без EMA и
другого состояния), поэтому вектор одинаков при обучении и в реальном времени.

Отличия от v1:
- окна в 30-минутных свечах (48 свечей = сутки, 336 = неделя);
- нет funding / premium / среднего размера сделки — они расходятся между Binance и
  Hyperliquid (сверка источников), а история обучения — в основном Binance;
- поток сделок: доля объёма, купленного агрессором (buy_ratio ∈ [0, 1]) для каждой свечи.
  Источник: Binance taker buy volume (история), Hyperliquid trade_flow (живые данные).

Ошибка данных (пропуск свечи, нет потока по свече, нулевой объём) → FeatureError → HOLD.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise

from app.domain.market import Candle
from app.features.pipeline import FeatureError, FeatureVector

FEATURE_SET_VERSION = "v2-30m"
STEP = timedelta(minutes=30)
LOOKBACK = 337  # rv_336: 336 доходностей
RETURN_LAGS = (1, 2, 4, 8, 16, 48)

FEATURE_NAMES: tuple[str, ...] = (
    *(f"ret_{k}" for k in RETURN_LAGS),
    "close_pos",
    "range_rel",
    "body",
    "dist_sma_48",
    "dist_sma_192",
    "slope_sma_48",
    "slope_sma_192",
    "rv_48",
    "rv_336",
    "rv_ratio",
    "atr_14_rel",
    "range_ratio_8_48",
    "volume_log_ratio_48",
    "volume_z_48",
    "trades_z_48",
    "buy_ratio_1",
    "buy_ratio_4",
    "buy_ratio_16",
    "buy_ratio_z_48",
    "delta_4",
    "delta_16",
    "delta_vs_price_16",
    "hour_sin",
    "hour_cos",
)


@dataclass(frozen=True, slots=True)
class FlowBar:
    """Свеча с долей объёма, купленного агрессором."""

    candle: Candle
    buy_ratio: float


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _std(xs: Sequence[float]) -> float:
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _z(xs: Sequence[float]) -> float:
    s = _std(xs)
    return 0.0 if s == 0 else (xs[-1] - _mean(xs)) / s


def check_window(bars: Sequence[FlowBar], step: timedelta = STEP) -> Sequence[FlowBar]:
    if len(bars) < LOOKBACK:
        raise FeatureError(f"нужно {LOOKBACK} закрытых свечей, есть {len(bars)}")
    window = bars[-LOOKBACK:]
    for b in window:
        c = b.candle
        if not c.is_closed:
            raise FeatureError(f"свеча {c.open_time.isoformat()} не закрыта")
        if c.close_time - c.open_time != step:
            raise FeatureError(f"свеча {c.open_time.isoformat()}: длина не равна {step}")
        if min(c.open, c.high, c.low, c.close) <= 0 or c.volume <= 0 or c.trades_count <= 0:
            raise FeatureError(f"свеча {c.open_time.isoformat()}: нет цены, объёма или сделок")
        if not (0.0 <= b.buy_ratio <= 1.0) or math.isnan(b.buy_ratio):
            raise FeatureError(f"свеча {c.open_time.isoformat()}: нет данных потока сделок")
    for prev, cur in pairwise(window):
        if cur.candle.open_time - prev.candle.open_time != step:
            raise FeatureError(f"пропуск свечей после {prev.candle.open_time.isoformat()}")
    return window


def compute_v2(bars: Sequence[FlowBar], step: timedelta = STEP) -> FeatureVector:
    window = check_window(bars, step)
    o = [float(b.candle.open) for b in window]
    h = [float(b.candle.high) for b in window]
    lo = [float(b.candle.low) for b in window]
    c = [float(b.candle.close) for b in window]
    v = [float(b.candle.volume) for b in window]
    n = [float(b.candle.trades_count) for b in window]
    br = [b.buy_ratio for b in window]
    log = math.log

    lr = [log(b / a) for a, b in pairwise(c)]
    rr = [(hi - low) / cl for hi, low, cl in zip(h, lo, c, strict=True)]
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    sma48, sma192 = _mean(c[-48:]), _mean(c[-192:])
    rv48, rv336 = _std(lr[-48:]), _std(lr[-336:])
    if rv336 == 0:
        raise FeatureError("нулевая волатильность за неделю")
    log_v = [log(x) for x in v]
    log_n = [log(x) for x in n]
    signed = [(2 * r - 1) * vol for r, vol in zip(br, v, strict=True)]  # buy − sell
    delta4 = math.fsum(signed[-4:]) / math.fsum(v[-4:])
    delta16 = math.fsum(signed[-16:]) / math.fsum(v[-16:])
    ret16 = log(c[-1] / c[-17])
    price16 = math.tanh(ret16 / (rv48 * 4)) if rv48 > 0 else 0.0  # нормированное движение
    decision = window[-1].candle.close_time
    hour = (decision.hour + decision.minute / 60) * 2 * math.pi / 24

    values = (
        *(log(c[-1] / c[-1 - k]) for k in RETURN_LAGS),
        (c[-1] - lo[-1]) / (h[-1] - lo[-1]) if h[-1] > lo[-1] else 0.5,
        rr[-1],
        log(c[-1] / o[-1]),
        log(c[-1] / sma48),
        log(c[-1] / sma192),
        log(sma48 / _mean(c[-56:-8])),
        log(sma192 / _mean(c[-208:-16])),
        rv48,
        rv336,
        rv48 / rv336,
        _mean(tr[-14:]) / c[-1],
        _mean(rr[-8:]) / _mean(rr[-48:]),
        log(v[-1] / _mean(v[-48:])),
        _z(log_v[-48:]),
        _z(log_n[-48:]),
        br[-1] - 0.5,
        _mean(br[-4:]) - 0.5,
        _mean(br[-16:]) - 0.5,
        _z(br[-48:]),
        delta4,
        delta16,
        delta16 - price16,
        math.sin(hour),
        math.cos(hour),
    )
    if len(values) != len(FEATURE_NAMES):
        raise AssertionError("число признаков v2 не совпадает с FEATURE_NAMES")
    for name, value in zip(FEATURE_NAMES, values, strict=True):
        if not math.isfinite(value):
            raise FeatureError(f"признак {name} не конечен: {value}")
    return FeatureVector(
        version=FEATURE_SET_VERSION,
        decision_time=decision,
        names=FEATURE_NAMES,
        values=tuple(values),
    )


def binance_buy_ratio(candle: Candle) -> float:
    """Доля объёма, купленного агрессором, из raw Binance ({"taker_buy": "…"})."""
    raw = candle.raw or {}
    if "taker_buy" not in raw or candle.volume <= 0:
        return math.nan
    return float(raw["taker_buy"]) / float(candle.volume)
