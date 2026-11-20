"""Честная проверка идей «спрос и предложение» на часовых свечах по кварталам.

Для каждого квартала 2024Q1 … : параметры выбираются на 12 предыдущих месяцах (только сделки,
закрытые до начала квартала, минимум MIN_TRAIN_TRADES), затем применяются к кварталу.
Одна позиция за раз. Сравнение: случайный вход той же частоты (стоп 2 ATR, выход по времени)
и «купил и держал».
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import fmean
from typing import Any

import numpy as np
import pandas as pd

from app.research.evaluation import Trade, summarize
from app.research.hourly_sim import Bars, Fill, Signal, non_overlapping, simulate
from app.research.sd_signals import (
    ImbalanceParams,
    WallParams,
    ZoneParams,
    atr,
    imbalance_signals,
    wall_signals,
    zone_signals,
)

MIN_TRAIN_TRADES = 20
RANDOM_REPEATS = 200

SignalFn = Callable[[pd.DataFrame, Any], list[Signal]]

IDEAS: dict[str, tuple[SignalFn, list[Any]]] = {
    "zones": (
        zone_signals,
        [
            ZoneParams(base=b, k=k, hold=hold, target=t)
            for b in (2, 4)
            for k in (2.0, 3.0)
            for hold in (24, 48)
            for t in ("2r", "hvn")
        ],
    ),
    "walls": (
        wall_signals,
        [
            WallParams(band=band, hold=hold, direction=d)
            for band in ("1", "2", "5")
            for hold in (24, 48)
            for d in ("follow", "fade")
        ],
    ),
    "imbalance": (
        imbalance_signals,
        [
            ImbalanceParams(days=days, hold=hold, direction=d)
            for days in (1, 3, 7)
            for hold in (24, 48)
            for d in ("follow", "fade")
        ],
    ),
}


def bars_of(h: pd.DataFrame) -> Bars:
    return Bars(
        h["open"].to_numpy(float),
        h["high"].to_numpy(float),
        h["low"].to_numpy(float),
        h["close"].to_numpy(float),
    )


def all_fills(h: pd.DataFrame, fn: SignalFn, params: Any) -> list[Fill]:
    bars = bars_of(h)
    return [f for s in fn(h, params) if (f := simulate(bars, s)) is not None]


def quarter_starts(
    first: pd.Timestamp, last: pd.Timestamp, start_year: int = 2024
) -> list[pd.Timestamp]:
    starts = []
    t = pd.Timestamp(datetime(start_year, 1, 1, tzinfo=UTC))
    while t < last:
        if t >= first:
            starts.append(t)
        t = t + pd.DateOffset(months=3)
    return starts


def _window(
    fills: Sequence[Fill],
    index: pd.DatetimeIndex,
    start: pd.Timestamp,
    end: pd.Timestamp,
    *,
    closed: bool,
) -> list[Fill]:
    """Сделки с сигналом в [start, end); closed — и выход до end (обучение без заглядывания)."""
    out = []
    for f in fills:
        t = index[f.signal.i]
        if start <= t < end and (not closed or index[f.exit_i] < end):
            out.append(f)
    return non_overlapping(out)


@dataclass
class QuarterResult:
    start: pd.Timestamp
    params: Any
    trades: list[Fill]
    train_mean: float | None
    buy_hold: float
    random_mean: float | None = None
    random_p: float | None = None  # доля случайных серий не хуже идеи


@dataclass
class IdeaResult:
    idea: str
    quarters: list[QuarterResult] = field(default_factory=list)

    def trades(self, index: pd.DatetimeIndex) -> list[Trade]:
        return [
            Trade(index[f.signal.i].to_pydatetime(), f.signal.side, f.net)
            for q in self.quarters
            for f in q.trades
        ]


def random_baseline(
    h: pd.DataFrame,
    start: pd.Timestamp,
    end: pd.Timestamp,
    n: int,
    hold: int,
    rng: np.random.Generator,
) -> list[float]:
    """Средний результат RANDOM_REPEATS серий из n случайных входов по рынку в квартале."""
    if n == 0:
        return []
    bars, a, close = bars_of(h), atr(h), h["close"].to_numpy(float)
    idx = np.flatnonzero((h.index >= start) & (h.index < end) & ~np.isnan(a))
    means = []
    for _ in range(RANDOM_REPEATS):
        nets = []
        for i in rng.choice(idx, size=n, replace=len(idx) < n):
            s = 1 if rng.random() < 0.5 else -1
            sig = Signal(
                i=int(i),
                side="long" if s > 0 else "short",
                entry="market",
                stop=close[i] - s * 2 * a[i],
                target=None,
                max_hours=hold,
            )
            if (f := simulate(bars, sig)) is not None:
                nets.append(f.net)
        if nets:
            means.append(fmean(nets))
    return means


def walk_forward(
    h: pd.DataFrame,
    idea: str,
    *,
    train_months: int = 12,
    seed: int = 7,
    progress: Callable[[str], None] | None = None,
) -> IdeaResult:
    fn, grid = IDEAS[idea]
    fills = {p: all_fills(h, fn, p) for p in grid}
    index = pd.DatetimeIndex(h.index)
    rng = np.random.default_rng(seed)
    result = IdeaResult(idea)
    close = h["close"]
    for start in quarter_starts(index[0], index[-1]):
        end = start + pd.DateOffset(months=3)
        train_start = start - pd.DateOffset(months=train_months)
        best, best_mean = None, None
        for p in grid:
            train = _window(fills[p], index, train_start, start, closed=True)
            if len(train) < MIN_TRAIN_TRADES:
                continue
            m = fmean(f.net for f in train)
            if best_mean is None or m > best_mean:
                best, best_mean = p, m
        q_close = close[(index >= start) & (index < end)]
        buy_hold = float(q_close.iloc[-1] / q_close.iloc[0] - 1) if len(q_close) else 0.0
        trades = _window(fills[best], index, start, end, closed=False) if best is not None else []
        q = QuarterResult(start, best, trades, best_mean, buy_hold)
        if trades:
            hold = getattr(best, "hold", 24)
            rand = random_baseline(h, start, end, len(trades), hold, rng)
            mean = fmean(f.net for f in trades)
            q.random_mean = fmean(rand) if rand else None
            q.random_p = sum(r >= mean for r in rand) / len(rand) if rand else None
        result.quarters.append(q)
        if progress:
            mean_txt = f"{fmean(f.net for f in trades):+.4f}" if trades else "—"
            progress(
                f"{idea} {start:%Y-%m}: {len(trades)} сделок, средний {mean_txt}, параметры {best}"
            )
    return result


def summary(result: IdeaResult, index: pd.DatetimeIndex) -> dict[str, Any]:
    trades = result.trades(index)
    qs = [q for q in result.quarters if q.trades]
    days = sum(
        (min(q.start + pd.DateOffset(months=3), index[-1]) - q.start).days for q in result.quarters
    )
    stats = summarize(trades, max(days, 1))
    stats["quarters"] = len(result.quarters)
    stats["quarters_traded"] = len(qs)
    stats["quarters_positive"] = sum(sum(f.net for f in q.trades) > 0 for q in qs)
    rand = [q.random_mean for q in qs if q.random_mean is not None]
    stats["random_mean"] = fmean(rand) if rand else None
    return stats
