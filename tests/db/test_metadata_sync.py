import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Network
from app.db.repositories.market import get_market
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.info_client import InfoClient
from app.market_data.metadata import sync_market

FIXTURES = Path(__file__).parents[1] / "hyperliquid" / "fixtures"
NOW = datetime(2026, 11, 17, tzinfo=UTC)


def gateway_with_meta(name: str, network: Network) -> HyperliquidGateway:
    meta = json.loads((FIXTURES / name).read_text())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=meta))
    return HyperliquidGateway(
        InfoClient("https://example.invalid", transport=transport), network, clock=lambda: NOW
    )


async def test_sync_market_stores_exchange_asset_id(session: AsyncSession) -> None:
    gw = gateway_with_meta("meta_testnet.json", Network.TESTNET)
    market_id, meta, rules = await sync_market(gw, session, "BTC")

    record = await get_market(session, Network.TESTNET, "BTC")
    assert record is not None and record.id == market_id
    assert record.asset_id == 3
    assert record.raw_meta["szDecimals"] == 5
    assert record.meta_updated_at == NOW
    assert rules.max_price_decimals == 1 and rules.asset_id == meta.asset_id


async def test_sync_market_refuses_delisted(session: AsyncSession) -> None:
    gw = gateway_with_meta("meta_flags.json", Network.MAINNET)
    with pytest.raises(RuntimeError, match="делистингован"):
        await sync_market(gw, session, "MATIC")
