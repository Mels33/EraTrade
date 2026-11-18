"""Целевая переменная (§14.2): класс доходности за горизонт с учётом издержек.

Вход — по close свечи решения (бот входит через секунды после её закрытия), выход —
по close через horizon свечей. Порог = издержки туда-обратно + запас + минимальный край
(TargetConfig.threshold_bps), чтобы модель не училась на шуме меньше издержек.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Literal, Protocol


class Label(StrEnum):
    DOWN = "DOWN"
    FLAT = "FLAT"
    UP = "UP"


# Фиксированный порядок классов: индексы вероятностей модели и столбцов метрик.
CLASSES: tuple[Label, ...] = (Label.DOWN, Label.FLAT, Label.UP)


def forward_return(entry_close: Decimal, exit_close: Decimal) -> float:
    """Простая доходность за горизонт (считается в Decimal, затем одно округление до float)."""
    if entry_close <= 0:
        raise ValueError("цена входа должна быть положительной")
    return float(exit_close / entry_close - 1)


def label_for(ret: float, threshold_bps: Decimal) -> Label:
    threshold = float(threshold_bps) / 10_000
    if ret > threshold:
        return Label.UP
    if ret < -threshold:
        return Label.DOWN
    return Label.FLAT


# --- варианты разметки для эксперимента с целевой переменной ---


class _Bar(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class TargetSpec:
    """Как размечать строку набора.

    fixed   — доходность за horizon против ±threshold_bps (текущая спецификация);
    vol     — против ±max(floor_bps, k·σ), σ = часовая волатильность (rv_24) · √horizon;
    barrier — что раньше за horizon свечей: +D, −D или ничего; D = max(atr_mult·ATR14,
              min_distance) — та же дистанция, что у защитного стопа бота.
    """

    name: str
    kind: Literal["fixed", "vol", "barrier"]
    horizon: int
    threshold_bps: Decimal = Decimal("39.5")
    floor_bps: Decimal = Decimal("19.5")
    k: float = 1.0
    atr_mult: float = 1.5
    min_distance: float = 0.005


@dataclass(frozen=True, slots=True)
class Outcome:
    """Метка и результат сделки, начатой по close свечи решения (без издержек)."""

    label: Label
    long_return: float
    short_return: float


def _ret(entry: Decimal, exit_: Decimal) -> float:
    return float(exit_ / entry - 1)


def label_fixed(bars: Sequence[_Bar], i: int, horizon: int, threshold_bps: Decimal) -> Outcome:
    r = _ret(bars[i].close, bars[i + horizon].close)
    return Outcome(label_for(r, threshold_bps), r, -r)


def label_vol(
    bars: Sequence[_Bar],
    i: int,
    horizon: int,
    *,
    sigma_1h: float,
    k: float,
    floor_bps: Decimal,
) -> Outcome:
    """Порог от волатильности, известной в момент решения (rv_24 — только прошлые свечи)."""
    r = _ret(bars[i].close, bars[i + horizon].close)
    threshold = max(float(floor_bps) / 10_000, k * sigma_1h * horizon**0.5)
    if r > threshold:
        label = Label.UP
    elif r < -threshold:
        label = Label.DOWN
    else:
        label = Label.FLAT
    return Outcome(label, r, -r)


def label_barrier(bars: Sequence[_Bar], i: int, horizon: int, *, distance: float) -> Outcome:
    """Triple barrier по high/low будущих свечей.

    UP — +D достигнута раньше −D (long закрылся бы по тейк-профиту, short — по стопу),
    DOWN — наоборот. Оба барьера в одной свече — порядок внутри часа неизвестен: FLAT,
    а результат обеих сторон — стоп (консервативно). Ни одного — FLAT, выход по close.
    """
    entry = bars[i].close
    up = float(entry) * (1 + distance)
    down = float(entry) * (1 - distance)
    for j in range(1, horizon + 1):
        bar = bars[i + j]
        hit_up, hit_down = float(bar.high) >= up, float(bar.low) <= down
        if hit_up and hit_down:
            return Outcome(Label.FLAT, -distance, -distance)
        if hit_up:
            return Outcome(Label.UP, distance, -distance)
        if hit_down:
            return Outcome(Label.DOWN, -distance, distance)
    r = _ret(entry, bars[i + horizon].close)
    return Outcome(Label.FLAT, r, -r)
