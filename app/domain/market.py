"""Доменные типы рыночных данных. Не зависят ни от SDK, ни от ORM."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.config.settings import Network


class DataSource(StrEnum):
    REST = "rest"
    WS = "ws"
    IMPORT = "import"  # внешний исторический источник


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} должен быть timezone-aware (UTC)")


@dataclass(frozen=True, slots=True)
class MarketMeta:
    network: Network
    coin: str
    asset_id: int
    sz_decimals: int
    max_leverage: int
    only_isolated: bool
    is_delisted: bool
    updated_at: datetime
    raw: dict[str, Any]
    dex: str = ""  # "" — основной perp DEX Hyperliquid
    margin_table_id: int | None = None

    def __post_init__(self) -> None:
        _require_aware("updated_at", self.updated_at)
        if self.asset_id < 0 or self.sz_decimals < 0 or self.max_leverage < 1:
            raise ValueError(f"некорректные метаданные рынка {self.coin}")


@dataclass(frozen=True, slots=True)
class Candle:
    interval: str
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    trades_count: int
    is_closed: bool
    source: DataSource
    received_at: datetime
    raw: dict[str, Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _require_aware("open_time", self.open_time)
        _require_aware("close_time", self.close_time)
        _require_aware("received_at", self.received_at)
        if self.close_time <= self.open_time:
            raise ValueError("close_time должен быть позже open_time")
        if not (self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise ValueError("нарушено low <= open/close <= high")
        if self.volume < 0 or self.trades_count < 0:
            raise ValueError("объём и число сделок не могут быть отрицательными")


@dataclass(frozen=True, slots=True)
class AssetContext:
    """Снимок funding / OI / mark / oracle (activeAssetCtx или metaAndAssetCtxs)."""

    mark_price: Decimal
    oracle_price: Decimal
    funding: Decimal
    open_interest: Decimal
    received_at: datetime
    source: DataSource
    mid_price: Decimal | None = None
    premium: Decimal | None = None
    day_notional_volume: Decimal | None = None
    impact_bid_price: Decimal | None = None
    impact_ask_price: Decimal | None = None
    exchange_time: datetime | None = None  # у activeAssetCtx биржевого времени нет
    raw: dict[str, Any] | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        _require_aware("received_at", self.received_at)
        if self.exchange_time is not None:
            _require_aware("exchange_time", self.exchange_time)
