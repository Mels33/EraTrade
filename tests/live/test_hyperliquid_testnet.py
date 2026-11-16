"""Живые тесты против публичного API Hyperliquid testnet (только чтение, без ключей).

Запуск: ERATRADE_LIVE_TESTS=1 uv run pytest tests/live
Нужны, чтобы быстро заметить изменение формата ответов биржи.
"""

import os
from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import Network
from app.domain.instruments import InstrumentRules, Rounding
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.info_client import InfoClient

pytestmark = pytest.mark.skipif(
    os.environ.get("ERATRADE_LIVE_TESTS") != "1", reason="ERATRADE_LIVE_TESTS=1 не задан"
)

TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
ZERO_ADDRESS = "0x" + "0" * 40


async def test_market_data_formats() -> None:
    async with InfoClient(TESTNET_URL) as client:
        gw = HyperliquidGateway(client, Network.TESTNET)
        markets, contexts = await gw.get_markets_with_contexts()
        btc = next(m for m in markets if m.coin == "BTC")
        assert contexts["BTC"].mark_price > 0
        assert (await gw.get_all_mids())["BTC"] > 0

        now = datetime.now(UTC)
        candles = await gw.get_candles("BTC", "1h", start=now - timedelta(hours=6))
        assert 5 <= len(candles) <= 7
        assert all(c.is_closed for c in candles[:-1])

        rules = InstrumentRules.for_perp(btc)
        assert rules.is_valid_price(rules.round_price(contexts["BTC"].mark_price, Rounding.DOWN))


async def test_account_formats_on_empty_address() -> None:
    async with InfoClient(TESTNET_URL) as client:
        gw = HyperliquidGateway(client, Network.TESTNET)
        state = await gw.get_account_state(ZERO_ADDRESS)
        assert state.exchange_time.tzinfo is UTC
        await gw.get_open_orders(ZERO_ADDRESS)
        await gw.get_fills(ZERO_ADDRESS, start=datetime.now(UTC) - timedelta(days=1))
        assert not (await gw.get_order_status(ZERO_ADDRESS, 1)).found
        assert (await gw.get_user_rate_limit(ZERO_ADDRESS)).requests_cap >= 0
