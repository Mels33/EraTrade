"""Агрегаторы потока сделок и стакана, разбор WS-сообщений trades / l2Book."""

from datetime import timedelta
from decimal import Decimal

import pytest

from app.integrations.hyperliquid.errors import HyperliquidFormatError
from app.integrations.hyperliquid.parsing import datetime_to_ms, parse_ws_l2book, parse_ws_trades
from app.market_data.flow import BookAggregator, BookSnapshot, FlowAggregator, Trade, bucket_start
from tests.factories import T0

STEP = timedelta(minutes=30)
START = T0 - timedelta(minutes=1)  # сбор начат за минуту до корзины T0


def trade(minutes: float, tid: int, buy: bool = True, sz: str = "1", px: str = "100000") -> Trade:
    return Trade(T0 + timedelta(minutes=minutes), Decimal(px), Decimal(sz), buy, tid)


def test_bucket_start() -> None:
    assert bucket_start(T0 + timedelta(minutes=44), STEP) == T0 + STEP


def test_flow_dedup_large_and_completeness() -> None:
    agg = FlowAggregator(STEP, started_at=START, large_notional=Decimal(150_000))
    assert agg.add([trade(-5, 1)]) == 0  # раньше старта сбора
    agg.add([trade(1, 2), trade(2, 3, buy=False, sz="2"), trade(1, 2)])  # tid 2 — дубль
    agg.add([trade(31, 4)])
    assert agg.pop_closed(T0 + STEP) == []  # ждём grace после конца корзины
    (b,) = agg.pop_closed(T0 + STEP + timedelta(seconds=5))
    assert b.complete and b.buy_count == 1 and b.sell_count == 1
    assert b.sell_volume == Decimal(2) and b.large_sell_notional == Decimal(200_000)
    assert b.large_buy_notional == 0  # 100 000 < порога 150 000


def test_bucket_started_mid_way_is_incomplete() -> None:
    agg = FlowAggregator(STEP, started_at=T0 + timedelta(minutes=10))
    agg.add([trade(11, 1)])
    (b,) = agg.pop_closed(T0 + STEP + timedelta(seconds=5))
    assert not b.complete


def test_gap_marks_all_touched_buckets() -> None:
    agg = FlowAggregator(STEP, started_at=START)
    agg.add([trade(1, 1)])
    agg.mark_gap(T0 + timedelta(minutes=20), T0 + timedelta(minutes=70))  # задеты 3 корзины
    agg.add([trade(45, 2), trade(65, 3), trade(95, 4)])
    buckets = agg.pop_closed(T0 + 4 * STEP + timedelta(seconds=5))
    assert [b.complete for b in buckets] == [False, False, False, True]


def snap(minutes: float, bid: str = "3", ask: str = "1") -> BookSnapshot:
    bids = tuple((Decimal(99_999 - i), Decimal(bid)) for i in range(20))
    asks = tuple((Decimal(100_001 + i), Decimal(ask)) for i in range(20))
    return BookSnapshot(T0 + timedelta(minutes=minutes), bids, asks)


def test_book_sampling_and_stats() -> None:
    agg = BookAggregator(STEP, started_at=START, sample_seconds=10)
    assert agg.add(snap(1))
    assert not agg.add(snap(1.1))  # 6 с — чаще порога
    assert agg.add(snap(2, bid="1", ask="1"))
    (b,) = agg.pop_closed(T0 + STEP + timedelta(seconds=5))
    assert b.samples == 2 and b.complete
    assert b.mean(b.imbalance5_sum) == pytest.approx(0.25)
    assert b.mean(b.spread_bps_sum) == pytest.approx(0.2)  # 2 / 100000
    assert b.last_imbalance5 == 0.0


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
