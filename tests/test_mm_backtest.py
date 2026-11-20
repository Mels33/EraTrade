"""Событийный бэктест маркет-мейкера: очередь, PnL, markout и импорт JSONL."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.config.strategy import MmConfig
from app.domain.account import Side
from app.domain.instruments import InstrumentRules
from app.market_data.flow import BookSnapshot, Trade
from app.mm.adverse import load_adverse_selection, save_adverse_selection
from app.research.mm_backtest import MarketMakerBacktest, read_events

D = Decimal
T0 = datetime(2026, 11, 1, tzinfo=UTC)
RULES = InstrumentRules("TEST", 0, 2, 4)
CFG = MmConfig(
    coins=("TEST",),
    order_usd=D(10),
    max_inventory_usd=D(20),
    min_spread_bps=D(5),
    improve_min_ticks=100,
    min_edge_bps=D(0),
    inventory_skew_bps=D(0),
    requote_min_interval_seconds=0,
    requote_slack_bps=D(0),
)


def book(at: float = 0, bid: str = "100", ask: str = "100.1", size: str = "5") -> BookSnapshot:
    return BookSnapshot(T0 + timedelta(seconds=at), ((D(bid), D(size)),), ((D(ask), D(size)),))


def trade(at: float, size: str, *, price: str = "100", buy: bool = False, tid: int = 1) -> Trade:
    return Trade(T0 + timedelta(seconds=at), D(price), D(size), buy, tid)


def backtest(**kwargs: object) -> MarketMakerBacktest:
    return MarketMakerBacktest("TEST", RULES, CFG, quote_latency_ms=0, **kwargs)  # type: ignore[arg-type]


def test_queue_is_consumed_before_partial_fill() -> None:
    result = backtest().run(
        [
            book(),
            trade(0.001, "3"),
            trade(0.002, "2", tid=2),
            trade(0.003, "0.04", tid=3),
            trade(0.004, "0.06", tid=4),
        ]
    )
    assert [(f.size, f.queue_ahead) for f in result.executions] == [
        (D("0.04"), D(0)),
        (D("0.06"), D(0)),
    ]
    assert result.trades == 4


def test_touch_fills_only_excess_and_duplicate_trade_is_ignored() -> None:
    result = backtest().run(
        [
            book(),
            trade(0.001, "5.04"),
            trade(0.001, "5.04"),
            trade(0.002, "0.06", tid=2),
        ]
    )
    assert [(f.size, f.queue_ahead) for f in result.executions] == [
        (D("0.04"), D(5)),
        (D("0.06"), D(0)),
    ]
    assert result.final_position == D("0.1")
    assert result.trades == 2


def test_trade_through_fills_without_waiting_for_queue() -> None:
    result = backtest().run([book(), trade(0.001, "0.01", price="99.99")])
    assert len(result.executions) == 1
    assert result.executions[0].size == D("0.1")
    assert result.final_position == D("0.1")


def test_cancelled_external_size_does_not_advance_queue() -> None:
    result = backtest().run(
        [
            book(),
            trade(0.001, "3"),
            book(0.002, size="0"),
            trade(0.003, "0.01", tid=2),
        ]
    )
    assert result.executions == ()


def test_round_trip_pnl_fees_and_short_markout() -> None:
    result = backtest(markout_seconds=(1, 5)).run(
        [
            book(),
            trade(0.001, "0.01", price="99.99"),
            trade(0.002, "5.1", price="100.1", buy=True, tid=2),
            book(2, bid="100.2", ask="100.3"),
        ]
    )
    assert [f.side for f in result.executions] == [Side.BUY, Side.SELL]
    assert result.final_position == 0
    assert result.gross_realized_usd == D("0.010")
    assert result.fees_usd == D("0.00300150")
    assert result.net_pnl_usd == D("0.00699850")
    assert result.markouts[0].fills == 2
    assert result.markouts[1].fills == 0
    assert result.markouts[1].adverse_selection_bps == 0


def test_negative_buy_markout_becomes_selection_penalty(tmp_path: Path) -> None:
    result = backtest(markout_seconds=(1,)).run(
        [book(), trade(0.001, "0.01", price="99.99"), book(2, bid="99.85", ask="99.95")]
    )
    markout = result.markouts[0]
    assert markout.mean_bps == D("-10.0")
    assert markout.adverse_selection_bps == D("10.0")
    path = tmp_path / "adverse.json"
    save_adverse_selection(result, path, horizon_seconds=1)
    assert load_adverse_selection(path) == {"TEST": D("10.0")}


def test_read_events_merges_ws_and_node_fill_jsonl(tmp_path: Path) -> None:
    book_path = tmp_path / "book.jsonl"
    trade_path = tmp_path / "trades.jsonl"
    book_path.write_text(
        json.dumps(
            {
                "channel": "l2Book",
                "data": {
                    "coin": "TEST",
                    "time": int(T0.timestamp() * 1000),
                    "levels": [[{"px": "100", "sz": "5"}], [{"px": "100.1", "sz": "5"}]],
                },
            }
        )
        + "\n"
    )
    trade_path.write_text(
        json.dumps(
            {
                "fills": [
                    {
                        "coin": "TEST",
                        "side": "A",
                        "px": "99.99",
                        "sz": "0.01",
                        "time": int((T0 + timedelta(milliseconds=1)).timestamp() * 1000),
                        "tid": 1,
                    }
                ]
            }
        )
        + "\n"
    )
    result = MarketMakerBacktest("TEST", RULES, CFG, quote_latency_ms=0).run(
        read_events([book_path], [trade_path], "TEST")
    )
    assert result.books == 1
    assert result.trades == 1
    assert result.fills == 1


def test_unsorted_events_rejected() -> None:
    with pytest.raises(ValueError, match="отсортированы"):
        backtest().run([book(1), book(0)])
