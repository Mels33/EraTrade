"""Построители тестовых доменных объектов."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config.settings import Network
from app.domain.market import AssetContext, Candle, DataSource, MarketMeta

T0 = datetime(2026, 11, 1, tzinfo=UTC)
HOUR = timedelta(hours=1)


def market_meta(**overrides: Any) -> MarketMeta:
    values: dict[str, Any] = {
        "network": Network.TESTNET,
        "coin": "BTC",
        "asset_id": 3,
        "sz_decimals": 5,
        "max_leverage": 40,
        "only_isolated": False,
        "is_delisted": False,
        "updated_at": T0,
        "raw": {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    }
    values.update(overrides)
    return MarketMeta(**values)


def candle(index: int = 0, **overrides: Any) -> Candle:
    open_time = T0 + index * HOUR
    values: dict[str, Any] = {
        "interval": "1h",
        "open_time": open_time,
        "close_time": open_time + HOUR,  # конец интервала (исключительно)
        "open": Decimal("90000"),
        "high": Decimal("90500.5"),
        "low": Decimal("89800"),
        "close": Decimal("90200"),
        "volume": Decimal("123.45678"),
        "trades_count": 1000,
        "is_closed": True,
        "source": DataSource.REST,
        "received_at": open_time + HOUR + timedelta(seconds=2),
        "raw": {"t": int(open_time.timestamp() * 1000)},
    }
    values.update(overrides)
    return Candle(**values)


def asset_context(**overrides: Any) -> AssetContext:
    values: dict[str, Any] = {
        "mark_price": Decimal("90100"),
        "oracle_price": Decimal("90090"),
        "funding": Decimal("0.0000125"),
        "open_interest": Decimal("12345.678"),
        "received_at": T0,
        "source": DataSource.WS,
    }
    values.update(overrides)
    return AssetContext(**values)
