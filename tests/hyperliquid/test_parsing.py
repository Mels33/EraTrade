"""Парсеры на реальных (урезанных) ответах Hyperliquid из tests/hyperliquid/fixtures."""

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.settings import Network
from app.domain.account import Side
from app.domain.market import DataSource
from app.integrations.hyperliquid import parsing
from app.integrations.hyperliquid.errors import HyperliquidFormatError

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 17, tzinfo=UTC)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def test_meta_asset_id_is_universe_index() -> None:
    markets = parsing.parse_meta(load("meta_testnet.json"), Network.TESTNET, NOW)
    btc = next(m for m in markets if m.coin == "BTC")
    assert btc.asset_id == 3  # на testnet BTC не первый — asset ID нельзя хардкодить
    assert btc.sz_decimals == 5
    assert btc.max_leverage == 40
    assert btc.network is Network.TESTNET
    assert btc.raw["name"] == "BTC"
    assert [m.asset_id for m in markets] == list(range(len(markets)))


def test_meta_flags() -> None:
    markets = {m.coin: m for m in parsing.parse_meta(load("meta_flags.json"), Network.MAINNET, NOW)}
    assert markets["BTC"].asset_id == 0
    assert not markets["BTC"].only_isolated and not markets["BTC"].is_delisted
    assert markets["HPOS"].only_isolated and markets["HPOS"].is_delisted
    assert markets["MATIC"].is_delisted and not markets["MATIC"].only_isolated


def test_meta_and_contexts() -> None:
    markets, ctxs = parsing.parse_meta_and_contexts(
        load("meta_and_asset_ctxs_mainnet.json"), Network.MAINNET, NOW
    )
    btc = ctxs["BTC"]
    assert len(ctxs) == len(markets)
    assert btc.mark_price == Decimal("64974.0")
    assert btc.oracle_price == Decimal("65005.0")
    assert btc.funding == Decimal("0.0000075224")
    assert btc.impact_bid_price == Decimal("64973.0")
    assert btc.source is DataSource.REST
    assert btc.received_at == NOW


def test_meta_and_contexts_length_mismatch() -> None:
    data = load("meta_and_asset_ctxs_mainnet.json")
    data[1] = data[1][:-1]
    with pytest.raises(HyperliquidFormatError, match="число контекстов"):
        parsing.parse_meta_and_contexts(data, Network.MAINNET, NOW)


def test_candle_snapshot_marks_last_candle_open() -> None:
    raw = load("candle_snapshot_btc_1h.json")
    last_end = parsing.ms_to_datetime(raw[-1]["T"]) + timedelta(milliseconds=1)
    received = last_end - timedelta(minutes=5)  # текущая свеча ещё идёт

    candles = parsing.parse_candle_snapshot(
        raw, coin="BTC", interval="1h", received_at=received, close_grace=timedelta(seconds=10)
    )
    assert [c.is_closed for c in candles] == [True, True, True, False]
    first = candles[0]
    assert first.close_time - first.open_time == timedelta(hours=1)
    assert first.open == Decimal(raw[0]["o"])
    assert first.trades_count == raw[0]["n"]
    assert first.source is DataSource.REST


def test_candle_closes_only_after_grace() -> None:
    raw = load("candle_snapshot_btc_1h.json")[-1:]
    end = parsing.ms_to_datetime(raw[0]["T"]) + timedelta(milliseconds=1)
    grace = timedelta(seconds=10)

    def closed(at: datetime) -> bool:
        return parsing.parse_candle_snapshot(
            raw, coin="BTC", interval="1h", received_at=at, close_grace=grace
        )[0].is_closed

    assert not closed(end + timedelta(seconds=9))
    assert closed(end + grace)


def test_candle_snapshot_rejects_null_and_foreign_candles() -> None:
    with pytest.raises(HyperliquidFormatError, match="null"):
        parsing.parse_candle_snapshot(
            None, coin="NOPE", interval="1h", received_at=NOW, close_grace=timedelta(0)
        )
    raw = load("candle_snapshot_btc_1h.json")
    with pytest.raises(HyperliquidFormatError, match="чужая свеча"):
        parsing.parse_candle_snapshot(
            raw, coin="ETH", interval="1h", received_at=NOW, close_grace=timedelta(0)
        )


def test_clearinghouse_state() -> None:
    state = parsing.parse_clearinghouse_state(load("clearinghouse_state.json"))
    assert state.account_value == Decimal("3226639.6319630002")
    assert len(state.positions) == 2
    btc = state.position("BTC")
    assert btc is not None
    assert btc.size == Decimal("-0.08641") and not btc.is_long
    assert btc.leverage_type == "cross" and btc.leverage == 20
    assert btc.funding_since_open == Decimal("-2.102334")
    assert state.exchange_time.tzinfo is UTC


def test_isolated_position_with_null_liquidation_price() -> None:
    data = load("clearinghouse_state.json")
    pos = data["assetPositions"][0]["position"]
    pos["leverage"] = {"type": "isolated", "value": 2, "rawUsd": "-100.5"}
    pos["liquidationPx"] = None
    state = parsing.parse_clearinghouse_state(data)
    assert state.positions[0].leverage_type == "isolated"
    assert state.positions[0].liquidation_price is None


def test_open_orders() -> None:
    orders = parsing.parse_open_orders(load("frontend_open_orders.json"))
    o = orders[0]
    assert o.side is Side.BUY
    assert o.order_type == "Limit" and o.tif == "Alo"
    assert o.cloid is None and o.trigger_price is None
    assert not o.reduce_only


def test_trigger_order() -> None:
    raw = load("frontend_open_orders.json")[0]
    raw.update(
        side="A",
        isTrigger=True,
        triggerPx="60000.0",
        triggerCondition="Price below 60000",
        orderType="Stop Market",
        reduceOnly=True,
        cloid="0x" + "ab" * 16,
        tif=None,
    )
    o = parsing.parse_open_order(raw)
    assert o.side is Side.SELL
    assert o.is_trigger and o.trigger_price == Decimal("60000.0")
    assert o.reduce_only and o.cloid == "0x" + "ab" * 16


def test_fills() -> None:
    fills = parsing.parse_fills(load("user_fills.json"))
    f = fills[0]
    assert f.direction == "Close Short"
    assert f.fee == Decimal("0.002405")
    assert f.tid == 676093254779938
    assert f.side is Side.BUY


def test_order_status() -> None:
    found = parsing.parse_order_status(load("order_status_open.json"))
    assert found.found and found.status == "open"
    assert found.order is not None and found.order.oid == 607464776236
    assert not parsing.parse_order_status(load("order_status_unknown.json")).found
    with pytest.raises(HyperliquidFormatError, match="неизвестный status"):
        parsing.parse_order_status({"status": "weird"})


def test_user_rate_limit() -> None:
    rl = parsing.parse_user_rate_limit(load("user_rate_limit.json"))
    assert rl.requests_cap > rl.requests_used
    assert rl.remaining == rl.requests_cap + rl.requests_surplus - rl.requests_used


def test_all_mids() -> None:
    mids = parsing.parse_all_mids(load("all_mids.json"))
    assert all(isinstance(v, Decimal) for v in mids.values())


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("marginSummary", "accountValue"), None),
        (("marginSummary", "accountValue"), "abc"),
        (("marginSummary", "accountValue"), 1.5),  # float — потеря точности недопустима
        (("marginSummary", "accountValue"), "NaN"),
        (("time",), "1794863960517"),
        (("assetPositions",), {}),
    ],
)
def test_unexpected_format_raises(path: tuple[str, ...], value: object) -> None:
    data = copy.deepcopy(load("clearinghouse_state.json"))
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(HyperliquidFormatError):
        parsing.parse_clearinghouse_state(data)


def test_missing_field_raises() -> None:
    data = load("user_fills.json")
    del data[0]["tid"]
    with pytest.raises(HyperliquidFormatError, match="tid"):
        parsing.parse_fills(data)


def test_unknown_side_raises() -> None:
    data = load("user_fills.json")
    data[0]["side"] = "X"
    with pytest.raises(HyperliquidFormatError, match="side"):
        parsing.parse_fills(data)
