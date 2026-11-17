"""Feature pipeline v1 (§14.1 плана).

Признаки — чистая функция фиксированного окна: последние LOOKBACK_CANDLES закрытых свечей
и последние FUNDING_WINDOW начислений funding, известных к моменту решения. Нет EMA и
другого состояния, зависящего от всей истории, поэтому вектор для момента t одинаков
при обучении (по всей истории) и в реальном времени (по окну из БД) — бит в бит.

Момент решения (decision_time) — конец последней закрытой свечи. Funding берётся строго
до него: начисление на границе часа публикуется через десятки миллисекунд после неё.

Любое нарушение (пропуск свечи, незакрытая свеча, устаревший funding, нечисловое значение)
даёт FeatureError — политика торговли превращает его в HOLD.
"""

import hashlib
import json
import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from app.domain.market import Candle, FundingPoint
from app.integrations.hyperliquid.parsing import datetime_to_ms

FEATURE_SET_VERSION = "v1"
LOOKBACK_CANDLES = 169  # rv_168: 168 часовых доходностей
FUNDING_WINDOW = 24
MAX_FUNDING_AGE = timedelta(hours=2)  # последнее начисление не старше (к моменту решения)
MAX_FUNDING_SPAN = timedelta(hours=26)  # 24 начисления без крупных дыр

RETURN_LAGS = (1, 2, 4, 8, 16, 24)

FEATURE_NAMES: tuple[str, ...] = (
    *(f"ret_{k}" for k in RETURN_LAGS),
    "close_pos",
    "range_rel",
    "body",
    "dist_sma_24",
    "dist_sma_96",
    "slope_sma_24",
    "slope_sma_96",
    "rv_24",
    "rv_168",
    "rv_ratio",
    "atr_14_rel",
    "range_ratio_4_24",
    "volume_log_ratio_24",
    "volume_z_24",
    "trades_z_24",
    "trade_size_log_ratio_24",
    "funding_last_bps",
    "funding_mean_24_bps",
    "premium_last_bps",
    "premium_mean_24_bps",
    "hour_sin",
    "hour_cos",
)


class FeatureError(ValueError):
    """Признаки нельзя посчитать надёжно — решение должно быть HOLD."""


def pipeline_code_hash() -> str:
    """Хеш исходного кода pipeline: часть версии признаков в артефакте модели."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _canon(value: Decimal) -> str:
    """Каноническая запись Decimal: 90000 и 90000.0 дают одну строку."""
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


@dataclass(frozen=True, slots=True)
class FeatureInput:
    """Ровно те данные, из которых считается вектор (проверенные и обрезанные)."""

    decision_time: datetime
    candles: tuple[Candle, ...]
    funding: tuple[FundingPoint, ...]

    def hash(self) -> str:
        payload = {
            "version": FEATURE_SET_VERSION,
            "decision_time": datetime_to_ms(self.decision_time),
            "candles": [
                [
                    datetime_to_ms(c.open_time),
                    _canon(c.open),
                    _canon(c.high),
                    _canon(c.low),
                    _canon(c.close),
                    _canon(c.volume),
                    c.trades_count,
                ]
                for c in self.candles
            ],
            "funding": [
                [datetime_to_ms(f.time), _canon(f.funding_rate), _canon(f.premium)]
                for f in self.funding
            ],
        }
        raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class FeatureVector:
    version: str
    decision_time: datetime
    names: tuple[str, ...]
    values: tuple[float, ...]

    def as_dict(self) -> dict[str, float]:
        return dict(zip(self.names, self.values, strict=True))


def prepare_input(
    candles: Sequence[Candle],
    funding: Sequence[FundingPoint],
    *,
    step: timedelta = timedelta(hours=1),
) -> FeatureInput:
    """Проверяет данные и берёт окно, заканчивающееся последней переданной свечой.

    candles — по возрастанию open_time, последняя — свеча, после закрытия которой
    принимается решение. funding — по возрастанию времени, может быть с запасом:
    используются только начисления строго до момента решения.
    """
    if len(candles) < LOOKBACK_CANDLES:
        raise FeatureError(f"нужно {LOOKBACK_CANDLES} закрытых свечей, есть {len(candles)}")
    window = tuple(candles[-LOOKBACK_CANDLES:])
    for c in window:
        if not c.is_closed:
            raise FeatureError(f"свеча {c.open_time.isoformat()} не закрыта")
        if c.close_time - c.open_time != step:
            raise FeatureError(f"свеча {c.open_time.isoformat()}: длина не равна {step}")
        if min(c.open, c.high, c.low, c.close) <= 0:
            raise FeatureError(f"свеча {c.open_time.isoformat()}: неположительная цена")
        if c.volume <= 0 or c.trades_count <= 0:
            raise FeatureError(f"свеча {c.open_time.isoformat()}: нет объёма или сделок")
    for prev, cur in pairwise(window):
        if cur.open_time - prev.open_time != step:
            raise FeatureError(f"пропуск свечей после {prev.open_time.isoformat()}")

    decision_time = window[-1].close_time
    known = [f for f in funding if f.time < decision_time]
    if len(known) < FUNDING_WINDOW:
        raise FeatureError(f"нужно {FUNDING_WINDOW} начислений funding, есть {len(known)}")
    used = tuple(known[-FUNDING_WINDOW:])
    if any(b.time <= a.time for a, b in pairwise(used)):
        raise FeatureError("funding не упорядочен по времени")
    if decision_time - used[-1].time > MAX_FUNDING_AGE:
        raise FeatureError(f"funding устарел: последнее начисление {used[-1].time.isoformat()}")
    if decision_time - used[0].time > MAX_FUNDING_SPAN:
        raise FeatureError("в funding пропуски: 24 начисления растянуты больше чем на 26 ч")
    return FeatureInput(decision_time=decision_time, candles=window, funding=used)


def _mean(xs: Sequence[float]) -> float:
    return math.fsum(xs) / len(xs)


def _std(xs: Sequence[float]) -> float:
    m = _mean(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _z(xs: Sequence[float]) -> float:
    s = _std(xs)
    return 0.0 if s == 0 else (xs[-1] - _mean(xs)) / s


def compute_features(inp: FeatureInput) -> FeatureVector:
    o = [float(x.open) for x in inp.candles]
    h = [float(x.high) for x in inp.candles]
    lo = [float(x.low) for x in inp.candles]
    c = [float(x.close) for x in inp.candles]
    v = [float(x.volume) for x in inp.candles]
    n = [float(x.trades_count) for x in inp.candles]
    log = math.log

    lr = [log(b / a) for a, b in pairwise(c)]
    rr = [(hi - low) / cl for hi, low, cl in zip(h, lo, c, strict=True)]
    tr = [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])) for i in range(1, len(c))]
    sma24, sma96 = _mean(c[-24:]), _mean(c[-96:])
    rv24, rv168 = _std(lr[-24:]), _std(lr[-168:])
    if rv168 == 0:
        raise FeatureError("нулевая волатильность за 168 свечей")
    log_v = [log(x) for x in v]
    log_n = [log(x) for x in n]
    trade_size = [vi / ni for vi, ni in zip(v, n, strict=True)]
    rates = [float(f.funding_rate) * 1e4 for f in inp.funding]
    premiums = [float(f.premium) * 1e4 for f in inp.funding]
    hour = inp.decision_time.hour * 2 * math.pi / 24

    values = (
        *(log(c[-1] / c[-1 - k]) for k in RETURN_LAGS),
        (c[-1] - lo[-1]) / (h[-1] - lo[-1]) if h[-1] > lo[-1] else 0.5,
        rr[-1],
        log(c[-1] / o[-1]),
        log(c[-1] / sma24),
        log(c[-1] / sma96),
        log(sma24 / _mean(c[-28:-4])),
        log(sma96 / _mean(c[-104:-8])),
        rv24,
        rv168,
        rv24 / rv168,
        _mean(tr[-14:]) / c[-1],
        _mean(rr[-4:]) / _mean(rr[-24:]),
        log(v[-1] / _mean(v[-24:])),
        _z(log_v[-24:]),
        _z(log_n[-24:]),
        log(trade_size[-1] / _mean(trade_size[-24:])),
        rates[-1],
        _mean(rates),
        premiums[-1],
        _mean(premiums),
        math.sin(hour),
        math.cos(hour),
    )
    if len(values) != len(FEATURE_NAMES):
        raise AssertionError("число признаков не совпадает с FEATURE_NAMES")
    for name, value in zip(FEATURE_NAMES, values, strict=True):
        if not math.isfinite(value):
            raise FeatureError(f"признак {name} не конечен: {value}")
    return FeatureVector(
        version=FEATURE_SET_VERSION,
        decision_time=inp.decision_time,
        names=FEATURE_NAMES,
        values=tuple(values),
    )


def features_for(
    candles: Sequence[Candle],
    funding: Sequence[FundingPoint],
    *,
    step: timedelta = timedelta(hours=1),
) -> tuple[FeatureInput, FeatureVector]:
    inp = prepare_input(candles, funding, step=step)
    return inp, compute_features(inp)
