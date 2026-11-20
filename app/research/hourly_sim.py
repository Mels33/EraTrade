"""Симуляция сделки по часовым свечам: вход по рынку или лимитом, стоп, цель, выход по времени.

Консервативно (как app/research/execution_model.py):
- сигнал известен на закрытии часа i; вход по рынку — по open часа i+1 (taker);
- лимит исполняется, только если цена прошла СКВОЗЬ него (low < price для long), maker;
- в часе исполнения лимита цель не засчитывается (порядок внутри часа неизвестен), стоп — да;
- стоп и цель в одном часе — стоп; гэп за стоп — выход по open (хуже стопа);
- цель — лимит maker (только сквозь), стоп и выход по времени — по рынку.
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.research.execution_model import Costs

Side = Literal["long", "short"]


@dataclass(frozen=True, slots=True)
class Signal:
    i: int  # индекс часа, на закрытии которого известен сигнал
    side: Side
    entry: Literal["market", "limit"]
    stop: float
    target: float | None
    max_hours: int
    price: float | None = None  # цена лимита
    valid_hours: int = 72  # сколько живёт лимит


@dataclass(frozen=True, slots=True)
class Fill:
    signal: Signal
    entry_i: int
    exit_i: int
    entry_price: float
    exit_price: float
    net: float


@dataclass(frozen=True, slots=True)
class Bars:
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray

    def __len__(self) -> int:
        return len(self.close)


DEFAULT_COSTS = Costs()


def simulate(bars: Bars, sig: Signal, costs: Costs = DEFAULT_COSTS) -> Fill | None:
    """None — лимит не исполнился или данных не хватает до конца сделки."""
    n = len(bars)
    long = sig.side == "long"
    sign = 1.0 if long else -1.0
    if sig.entry == "market":
        e = sig.i + 1
        if e >= n:
            return None
        entry = float(bars.open[e])
        entry_cost = costs.taker_side
        target_in_entry_bar = True
    else:
        if sig.price is None:
            raise ValueError("лимитный сигнал без цены")
        entry = sig.price
        e = -1
        for j in range(sig.i + 1, min(sig.i + 1 + sig.valid_hours, n)):
            if (bars.low[j] < entry) if long else (bars.high[j] > entry):
                e = j
                break
        if e < 0:
            return None
        entry_cost = costs.maker_fee
        target_in_entry_bar = False
    # Стоп не может быть по «плохую» сторону от входа.
    if (sig.stop >= entry) if long else (sig.stop <= entry):
        return None
    last = e + sig.max_hours
    if last >= n:
        return None
    for j in range(e, last + 1):
        o, hi, lo = float(bars.open[j]), float(bars.high[j]), float(bars.low[j])
        if j > e and ((o <= sig.stop) if long else (o >= sig.stop)):
            return _fill(sig, e, j, entry, o, entry_cost, costs.taker_side, sign)
        if (lo <= sig.stop) if long else (hi >= sig.stop):
            return _fill(sig, e, j, entry, sig.stop, entry_cost, costs.taker_side, sign)
        tgt = sig.target
        hit_target = tgt is not None and ((hi > tgt) if long else (lo < tgt))
        if tgt is not None and hit_target and (j > e or target_in_entry_bar):
            return _fill(sig, e, j, entry, tgt, entry_cost, costs.maker_fee, sign)
    return _fill(sig, e, last, entry, float(bars.close[last]), entry_cost, costs.taker_side, sign)


def _fill(
    sig: Signal,
    e: int,
    x: int,
    entry: float,
    exit_: float,
    entry_cost: float,
    exit_cost: float,
    sign: float,
) -> Fill:
    gross = sign * (exit_ / entry - 1)
    return Fill(sig, e, x, entry, exit_, gross - entry_cost - exit_cost)


def non_overlapping(fills: list[Fill]) -> list[Fill]:
    """Одна позиция за раз: сделки по порядку сигналов, пока открыта — новые пропускаем."""
    taken: list[Fill] = []
    busy_until = -1
    for f in sorted(fills, key=lambda f: (f.signal.i, f.entry_i)):
        if f.signal.i < busy_until:
            continue
        taken.append(f)
        busy_until = f.exit_i
    return taken
