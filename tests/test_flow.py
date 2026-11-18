"""Разбор WS-сообщений trades / l2Book Hyperliquid (нужны маркет-мейкеру)."""

from decimal import Decimal

import pytest

from app.integrations.hyperliquid.errors import HyperliquidFormatError
from app.integrations.hyperliquid.parsing import datetime_to_ms, parse_ws_l2book, parse_ws_trades
from tests.factories import T0


def test_parse_ws_trades_and_book() -> None:
    t = datetime_to_ms(T0)
    trades = parse_ws_trades(
        [
            {"coin": "BTC", "side": "A", "px": "65000.5", "sz": "0.1", "time": t, "tid": 7},
            {"coin": "ETH", "side": "B", "px": "3000", "sz": "1", "time": t, "tid": 8},
        ],
        coin="BTC",
    )
    assert len(trades) == 1 and not trades[0].is_buy and trades[0].price == Decimal("65000.5")
    with pytest.raises(HyperliquidFormatError):
        parse_ws_trades(
            [{"coin": "BTC", "side": "X", "px": "1", "sz": "1", "time": t, "tid": 1}], coin="BTC"
        )
    book = parse_ws_l2book(
        {
            "coin": "BTC",
            "time": t,
            "levels": [[{"px": "99", "sz": "2", "n": 1}], [{"px": "101", "sz": "1", "n": 1}]],
        },
        coin="BTC",
    )
    assert book is not None and book.bids == ((Decimal(99), Decimal(2)),)
    assert parse_ws_l2book({"coin": "ETH", "time": t, "levels": [[], []]}, coin="BTC") is None
