"""Модель исполнения сделки для исследования (не симулятор биржи).

Правила сделки одинаковы для всех вариантов: защитный стоп −D (как у бота), выход по
времени через horizon свечей; у вариантов barrier — ещё тейк-профит +D.

taker — вход по close свечи решения по рынку; все выходы по рынку; издержки за сделку
         = 2 × (комиссия taker + проскальзывание) + спред — как в текущей спецификации.
maker — вход post-only лимитом по close свечи решения (long — bid, short — ask). Заявка
         живёт одну следующую свечу и считается исполненной, только если цена прошла
         СКВОЗЬ лимит (low < limit для long): так учитываются и неисполненные заявки, и
         то, что исполняются чаще всего «плохие» входы. Тейк-профит — лимит maker
         (исполнен, если цена прошла сквозь +D); стоп и выход по времени — по рынку.

Консервативно: в свече исполнения входа тейк-профит не засчитывается (порядок внутри
свечи неизвестен), а стоп — засчитывается; если в одной свече задеты оба барьера — стоп.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal, Protocol


class _Bar(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class Costs:
    maker_fee: float = 0.00015
    taker_fee: float = 0.00045
    half_spread: float = 0.00005
    slippage: float = 0.0002

    @property
    def taker_side(self) -> float:
        """Издержки одной стороны по рынку: комиссия + половина спреда + проскальзывание."""
        return self.taker_fee + self.half_spread + self.slippage


CostModel = Literal["taker", "maker"]


def simulate(
    bars: Sequence[_Bar],
    i: int,
    *,
    side: Literal["long", "short"],
    horizon: int,
    distance: float,
    take_profit: bool,
    model: CostModel,
    costs: Costs = Costs(),
) -> float | None:
    """Чистый результат сделки (доля), None — maker-заявка не исполнилась."""
    sign = 1 if side == "long" else -1
    entry = float(bars[i].close)
    first = i + 1
    if model == "maker":
        fill = bars[first]
        filled = float(fill.low) < entry if side == "long" else float(fill.high) > entry
        if not filled:
            return None
        entry_cost = costs.maker_fee
        tp_cost = costs.maker_fee
    else:
        entry_cost = costs.taker_side
        tp_cost = costs.taker_side
    stop_cost = costs.taker_side
    stop = entry * (1 - sign * distance)
    target = entry * (1 + sign * distance)

    for j in range(first, i + horizon + 1):
        bar = bars[j]
        hi, low = float(bar.high), float(bar.low)
        hit_stop = low <= stop if side == "long" else hi >= stop
        # тейк-профит — лимит: исполнен, только если цена прошла сквозь уровень
        hit_tp = take_profit and (hi > target if side == "long" else low < target)
        if model == "maker" and j == first:
            hit_tp = False  # в свече исполнения входа порядок событий неизвестен
        if hit_stop:
            return -distance - entry_cost - stop_cost
        if hit_tp:
            return distance - entry_cost - tp_cost
    exit_ = float(bars[i + horizon].close)
    gross = sign * (exit_ / entry - 1)
    return gross - entry_cost - costs.taker_side
