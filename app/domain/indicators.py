"""Индикаторы на закрытых свечах (Decimal, без float)."""

from collections.abc import Sequence
from decimal import Decimal
from itertools import pairwise
from typing import Protocol


class OHLC(Protocol):
    @property
    def high(self) -> Decimal: ...
    @property
    def low(self) -> Decimal: ...
    @property
    def close(self) -> Decimal: ...


def true_ranges(candles: Sequence[OHLC]) -> list[Decimal]:
    """TR_i = max(high-low, |high-prev_close|, |low-prev_close|), начиная со второй свечи."""
    return [
        max(c.high - c.low, abs(c.high - prev.close), abs(c.low - prev.close))
        for prev, c in pairwise(candles)
    ]


def atr(candles: Sequence[OHLC], period: int) -> Decimal:
    """Простой ATR: среднее TR последних period свечей. Нужно period + 1 закрытых свечей."""
    if period < 1:
        raise ValueError("period >= 1")
    if len(candles) < period + 1:
        raise ValueError(f"для ATR({period}) нужно {period + 1} свечей, есть {len(candles)}")
    ranges = true_ranges(candles[-(period + 1) :])
    return sum(ranges, Decimal(0)) / period
