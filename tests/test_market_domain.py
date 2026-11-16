from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest

from tests.factories import T0, asset_context, candle, market_meta


def test_valid_candle() -> None:
    c = candle()
    assert c.close_time > c.open_time


def test_naive_datetime_rejected() -> None:
    naive = datetime(2026, 11, 1)  # noqa: DTZ001 — проверяем отказ от naive datetime
    with pytest.raises(ValueError, match="timezone-aware"):
        candle(open_time=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        asset_context(received_at=naive)
    with pytest.raises(ValueError, match="timezone-aware"):
        market_meta(updated_at=naive)


@pytest.mark.parametrize(
    "overrides",
    [
        {"high": Decimal("89000")},  # high ниже open/close
        {"low": Decimal("90300")},  # low выше open
        {"close": Decimal("91000")},  # close выше high
        {"volume": Decimal("-1")},
        {"trades_count": -1},
        {"close_time": T0},  # close_time == open_time
    ],
)
def test_invalid_candle_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        candle(**overrides)


def test_invalid_market_meta_rejected() -> None:
    with pytest.raises(ValueError):
        market_meta(max_leverage=0)
    with pytest.raises(ValueError):
        market_meta(asset_id=-1)
