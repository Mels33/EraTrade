"""Построители тестовых доменных объектов."""

import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config.settings import Network
from app.domain.market import AssetContext, Candle, DataSource, FundingPoint, MarketMeta

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


def candle_series(n: int, *, start: datetime = T0, seed: int = 7) -> list[Candle]:
    """Детерминированное случайное блуждание BTC: n закрытых часовых свечей подряд."""
    rng = random.Random(seed)  # noqa: S311 — воспроизводимые тестовые данные
    price = Decimal("90000")
    result = []
    for i in range(n):
        open_ = price
        close = (open_ * Decimal(str(1 + rng.gauss(0, 0.006)))).quantize(Decimal("0.1"))
        high = max(open_, close) + Decimal(str(round(rng.uniform(0, 300), 1)))
        low = min(open_, close) - Decimal(str(round(rng.uniform(0, 300), 1)))
        open_time = start + i * HOUR
        result.append(
            candle(
                0,
                open_time=open_time,
                close_time=open_time + HOUR,
                received_at=open_time + HOUR + timedelta(seconds=2),
                open=open_,
                high=high,
                low=low,
                close=close,
                volume=Decimal(str(round(rng.uniform(500, 3000), 5))),
                trades_count=rng.randint(5000, 40000),
            )
        )
        price = close
    return result


def funding_series(start: datetime, end: datetime, *, seed: int = 11) -> list[FundingPoint]:
    """Почасовые начисления funding на границах часов (+45 мс, как на бирже) в [start, end)."""
    rng = random.Random(seed)  # noqa: S311 — воспроизводимые тестовые данные
    result = []
    t = start.replace(minute=0, second=0, microsecond=0)
    while t < end:
        result.append(
            FundingPoint(
                time=t + timedelta(milliseconds=45),
                funding_rate=Decimal(str(round(rng.uniform(-0.00002, 0.00004), 10))),
                premium=Decimal(str(round(rng.uniform(-0.0006, 0.0006), 10))),
            )
        )
        t += HOUR
    return result
