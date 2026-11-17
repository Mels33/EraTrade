"""Целевая переменная (§14.2): класс доходности за горизонт с учётом издержек.

Вход — по close свечи решения (бот входит через секунды после её закрытия), выход —
по close через horizon свечей. Порог = издержки туда-обратно + запас + минимальный край
(TargetConfig.threshold_bps), чтобы модель не училась на шуме меньше издержек.
"""

from decimal import Decimal
from enum import StrEnum


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
