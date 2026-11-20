"""Скальпинг с усреднением на минутных свечах (исследование, без денег).

Цикл для long (short — зеркально):
1. без позиции стоит лимит на покупку на step ниже текущей цены (как bid маркет-мейкера);
2. после исполнения — следующая докупка на step ниже последней (до levels уровней, равные
   доли капитала);
3. продажа всей позиции лимитом, когда цена на tp выше средней цены входа;
4. после последней докупки цена ушла ещё на step против — стоп по рынку (stop=True), иначе
   держим до возврата.

Консервативно: лимиты исполняются только сквозь цену; в минуте исполнения докупки цель не
засчитывается; стоп раньше цели. Результат — в долях капитала (levels долей), equity
считается по рынку каждую минуту (для просадки).
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np

from app.research.execution_model import Costs

COSTS = Costs()


@dataclass(frozen=True, slots=True)
class AvgParams:
    step: float = 0.005
    levels: int = 3
    tp: float = 0.005
    stop: bool = True
    side: Literal["long", "short"] = "long"
    size_mult: float = 1.0  # каждая следующая докупка в size_mult раз больше


@dataclass(frozen=True, slots=True)
class AvgResult:
    params: AvgParams
    cycles: int
    wins: int
    total: float  # доля капитала за период
    max_drawdown: float
    worst_cycle: float
    max_levels_used: int
    equity: np.ndarray  # по минутам, доля капитала


def run(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, p: AvgParams, costs: Costs = COSTS
) -> AvgResult:
    sign = 1.0 if p.side == "long" else -1.0
    # доли капитала по уровням: 1, m, m², … (капитал — сумма всех уровней)
    weights = np.array([p.size_mult**k for k in range(p.levels)])
    units = weights / weights.sum()
    fills: list[float] = []  # цены исполненных уровней
    realized = 0.0
    cycles = wins = max_used = 0
    worst = 0.0
    equity = np.empty(len(close))
    limit = close[0] * (1 - sign * p.step)
    for i in range(len(close)):
        hi, lo = high[i], low[i]
        filled_now = False
        if fills:
            # стоп: после последнего уровня цена ушла ещё на step против
            stop_px = fills[-1] * (1 - sign * p.step)
            if (
                p.stop
                and len(fills) == p.levels
                and ((lo <= stop_px) if sign > 0 else (hi >= stop_px))
            ):
                pnl = sum(
                    units[k] * (sign * (stop_px / f - 1) - costs.maker_fee - costs.taker_side)
                    for k, f in enumerate(fills)
                )
                realized += pnl
                cycles, worst = cycles + 1, min(worst, pnl)
                fills = []
                limit = close[i] * (1 - sign * p.step)
                equity[i] = realized
                continue
        # докупка / первый вход — лимит сквозь цену
        if len(fills) < p.levels and ((lo < limit) if sign > 0 else (hi > limit)):
            fills.append(limit)
            filled_now = True
            max_used = max(max_used, len(fills))
            limit = limit * (1 - sign * p.step)
        if fills and not filled_now:
            w = units[: len(fills)]
            avg = float(np.dot(w, fills) / w.sum())  # средняя цена, взвешенная размерами
            target = avg * (1 + sign * p.tp)
            if (hi > target) if sign > 0 else (lo < target):
                pnl = sum(
                    units[k] * (sign * (target / f - 1) - 2 * costs.maker_fee)
                    for k, f in enumerate(fills)
                )
                realized += pnl
                cycles += 1
                wins += pnl > 0
                worst = min(worst, pnl)
                fills = []
                limit = close[i] * (1 - sign * p.step)
                equity[i] = realized
                continue
        if not fills:
            limit = close[i] * (1 - sign * p.step)  # без позиции лимит следует за ценой
        open_pnl = sum(units[k] * sign * (close[i] / f - 1) for k, f in enumerate(fills))
        equity[i] = realized + open_pnl
    peak = np.maximum.accumulate(np.r_[0.0, equity])[1:]
    return AvgResult(
        p, cycles, wins, realized + (equity[-1] - realized), float((equity - peak).min()),
        worst, max_used, equity,
    )  # fmt: skip
