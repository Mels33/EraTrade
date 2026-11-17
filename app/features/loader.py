"""Загрузка данных для признаков из БД: окно для момента решения и вся история для набора."""

from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import CandleRecord
from app.db.repositories import market as market_repo
from app.domain.market import Candle, DataSource, FundingPoint
from app.features.pipeline import (
    FUNDING_WINDOW,
    LOOKBACK_CANDLES,
    MAX_FUNDING_SPAN,
    FeatureError,
    FeatureInput,
    FeatureVector,
    features_for,
)


def candle_from_record(r: CandleRecord) -> Candle:
    return Candle(
        interval=r.interval,
        open_time=r.open_time,
        close_time=r.close_time,
        open=r.open,
        high=r.high,
        low=r.low,
        close=r.close,
        volume=r.volume,
        trades_count=r.trades_count,
        is_closed=r.is_closed,
        source=DataSource(r.source),
        received_at=r.received_at,
    )


async def load_history(
    session: AsyncSession,
    market_id: int,
    interval: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> tuple[list[Candle], list[FundingPoint]]:
    """Закрытые свечи с open_time в [start, end) и funding за тот же период (с запасом в
    начале — для окна признаков первой строки)."""
    records = await market_repo.get_candles(session, market_id, interval, start=start, end=end)
    candles = [candle_from_record(r) for r in records]
    funding_start = start - MAX_FUNDING_SPAN if start is not None else None
    funding = await market_repo.get_funding_history(
        session, market_id, start=funding_start, end=end
    )
    return candles, funding


async def features_at(
    session: AsyncSession,
    market_id: int,
    interval: str,
    step: timedelta,
    decision_time: datetime | None = None,
) -> tuple[FeatureInput, FeatureVector]:
    """Вектор признаков на момент решения decision_time (конец закрытой свечи).
    Без decision_time — по последней закрытой свече в БД."""
    if decision_time is None:
        last = await market_repo.get_last_closed_candle(session, market_id, interval)
        if last is None:
            raise FeatureError("в БД нет закрытых свечей")
        decision_time = last.close_time
    last_open = decision_time - step
    records = await market_repo.get_candles(
        session,
        market_id,
        interval,
        start=last_open - step * (LOOKBACK_CANDLES - 1),
        end=last_open + step,
    )
    candles = [candle_from_record(r) for r in records]
    if not candles or candles[-1].open_time != last_open:
        raise FeatureError(f"нет закрытой свечи, заканчивающейся в {decision_time.isoformat()}")
    funding = await market_repo.get_funding_history(
        session,
        market_id,
        start=decision_time - MAX_FUNDING_SPAN - step * 2,
        end=decision_time,
    )
    if len(funding) > FUNDING_WINDOW + 4:
        funding = funding[-(FUNDING_WINDOW + 4) :]
    return features_for(candles, funding, step=step)
