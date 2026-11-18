from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    UniqueConstraint,
    false,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

NETWORK_CHECK = "network in ('testnet', 'mainnet')"
SOURCE_CHECK = "source in ('rest', 'ws', 'import')"


class MarketRecord(Base):
    """Метаданные инструмента. asset_id берётся из meta биржи и не хардкодится."""

    __tablename__ = "markets"
    __table_args__ = (
        UniqueConstraint("network", "dex", "coin"),
        CheckConstraint(NETWORK_CHECK, name="network"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    network: Mapped[str]
    dex: Mapped[str] = mapped_column(server_default="")
    coin: Mapped[str]
    asset_id: Mapped[int]
    sz_decimals: Mapped[int] = mapped_column(SmallInteger)
    max_leverage: Mapped[int] = mapped_column(SmallInteger)
    only_isolated: Mapped[bool]
    is_delisted: Mapped[bool] = mapped_column(server_default=false())
    margin_table_id: Mapped[int | None]
    raw_meta: Mapped[dict[str, Any]]
    meta_updated_at: Mapped[datetime]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CandleRecord(Base):
    """Свечи. Уникальность: (рынок, интервал, open_time); закрытая свеча неизменяема."""

    __tablename__ = "candles"
    __table_args__ = (
        CheckConstraint(SOURCE_CHECK, name="source"),
        CheckConstraint("high >= low", name="high_low"),
        CheckConstraint("close_time > open_time", name="time_order"),
    )

    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), primary_key=True)
    interval: Mapped[str] = mapped_column(primary_key=True)
    open_time: Mapped[datetime] = mapped_column(primary_key=True)
    close_time: Mapped[datetime]
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal]
    trades_count: Mapped[int]
    is_closed: Mapped[bool]
    source: Mapped[str]
    received_at: Mapped[datetime]
    raw: Mapped[dict[str, Any] | None]
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MarketContextSnapshotRecord(Base):
    """Снимки mark/oracle/funding/OI."""

    __tablename__ = "market_context_snapshots"
    __table_args__ = (
        CheckConstraint(SOURCE_CHECK, name="source"),
        Index("ix_market_context_snapshots_market_id_received_at", "market_id", "received_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"))
    exchange_time: Mapped[datetime | None]
    received_at: Mapped[datetime]
    mark_price: Mapped[Decimal]
    oracle_price: Mapped[Decimal]
    mid_price: Mapped[Decimal | None]
    funding: Mapped[Decimal]
    open_interest: Mapped[Decimal]
    premium: Mapped[Decimal | None]
    day_notional_volume: Mapped[Decimal | None]
    impact_bid_price: Mapped[Decimal | None]
    impact_ask_price: Mapped[Decimal | None]
    source: Mapped[str]
    raw: Mapped[dict[str, Any] | None]


class FundingHistoryRecord(Base):
    """Почасовой funding рынка (fundingHistory). time — момент начисления на бирже."""

    __tablename__ = "funding_history"

    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), primary_key=True)
    time: Mapped[datetime] = mapped_column(primary_key=True)
    funding_rate: Mapped[Decimal]
    premium: Mapped[Decimal]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class TradeFlowRecord(Base):
    """Поток сделок по корзинам (агрессор покупает / продаёт). Копит коллектор."""

    __tablename__ = "trade_flow"

    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), primary_key=True)
    interval: Mapped[str] = mapped_column(primary_key=True)
    open_time: Mapped[datetime] = mapped_column(primary_key=True)
    complete: Mapped[bool]
    buy_volume: Mapped[Decimal]
    sell_volume: Mapped[Decimal]
    buy_count: Mapped[int]
    sell_count: Mapped[int]
    buy_notional: Mapped[Decimal]
    sell_notional: Mapped[Decimal]
    large_buy_notional: Mapped[Decimal]
    large_sell_notional: Mapped[Decimal]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BookStatsRecord(Base):
    """Статистика стакана по корзинам (снимки ~раз в 10 с). Копит коллектор."""

    __tablename__ = "book_stats"

    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), primary_key=True)
    interval: Mapped[str] = mapped_column(primary_key=True)
    open_time: Mapped[datetime] = mapped_column(primary_key=True)
    complete: Mapped[bool]
    samples: Mapped[int]
    spread_bps_mean: Mapped[float | None]
    imbalance_top5_mean: Mapped[float | None]
    imbalance_top20_mean: Mapped[float | None]
    depth_top20_usd_mean: Mapped[float | None]
    last_imbalance_top5: Mapped[float | None]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())
