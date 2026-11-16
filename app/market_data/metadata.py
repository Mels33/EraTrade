"""Синхронизация метаданных рынка с биржей."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.market import upsert_market
from app.domain.instruments import InstrumentRules
from app.domain.market import MarketMeta
from app.integrations.hyperliquid.gateway import HyperliquidGateway


async def sync_market(
    gateway: HyperliquidGateway, session: AsyncSession, coin: str
) -> tuple[int, MarketMeta, InstrumentRules]:
    """Загружает актуальные meta инструмента и сохраняет их.

    Возвращает (markets.id, meta, правила точности).
    """
    meta = await gateway.get_market(coin)
    if meta.is_delisted:
        raise RuntimeError(f"{coin} делистингован в сети {meta.network}")
    market_id = await upsert_market(session, meta)
    return market_id, meta, InstrumentRules.for_perp(meta)
