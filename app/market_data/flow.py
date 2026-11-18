"""Сделки и снимки стакана Hyperliquid из публичного WebSocket (trades, l2Book).

Используются маркет-мейкером в реальном времени: сторона агрессора из поля side сделки
(B — покупка по рынку, A — продажа), дедупликация по tid; снимок стакана — уровни
(цена, размер), лучшие первыми.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class Trade:
    time: datetime
    price: Decimal
    size: Decimal
    is_buy: bool  # агрессор купил
    tid: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    time: datetime
    bids: tuple[tuple[Decimal, Decimal], ...]  # (цена, размер), лучшие первыми
    asks: tuple[tuple[Decimal, Decimal], ...]
