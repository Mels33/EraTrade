"""Вспомогательный исторический источник: Binance USDⓈ-M Futures, BTCUSDT (§7.4 плана).

Только публичные данные, без ключа. Используется для обучающего набора, когда собственной
истории Hyperliquid мало (биржа отдаёт ~5000 часовых свечей). Торговля идёт только на
Hyperliquid — этот источник в реальном времени не используется.

Почасовой funding в масштабе Hyperliquid:
- Hyperliquid начисляет funding каждый час, Binance — раз в 8 ч; ставка Binance делится на 8
  (базовый уровень совпадает: 0.01% / 8 ч = 0.00125% / ч);
- точка на конец часа t+1h берёт последнюю ставку, рассчитанную к этому моменту, и premium
  index Binance на закрытии часа — аналог premium Hyperliquid (отклонение perp от индекса).
"""

import asyncio
import bisect
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from app.config.settings import Network
from app.domain.market import Candle, DataSource, FundingPoint, MarketMeta
from app.integrations.hyperliquid.parsing import datetime_to_ms, ms_to_datetime

BASE_URL = "https://fapi.binance.com"
SYMBOL = "BTCUSDT"
DEX = "binance-um"  # отдельный «рынок» в БД: network=mainnet, dex=binance-um, coin=BTC
KLINES_LIMIT = 1500
FUNDING_LIMIT = 1000
HOUR = timedelta(hours=1)
INTERVAL_STEPS = {"30m": timedelta(minutes=30), "1h": HOUR}
FUNDING_PERIOD_HOURS = 8


def binance_market_meta(coin: str, now: datetime) -> MarketMeta:
    return MarketMeta(
        network=Network.MAINNET,
        dex=DEX,
        coin=coin,
        asset_id=0,
        sz_decimals=3,
        max_leverage=1,
        only_isolated=False,
        is_delisted=False,
        updated_at=now,
        raw={"source": "binance-usdm-futures", "symbol": SYMBOL, "note": "только для обучения"},
    )


class BinanceError(RuntimeError):
    pass


class BinanceFuturesClient:
    def __init__(
        self,
        base_url: str = BASE_URL,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        pause: float = 0.2,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url,
            timeout=20,
            transport=transport,
            headers={"User-Agent": "eratrade-research/1"},
        )
        self._pause = pause

    async def __aenter__(self) -> "BinanceFuturesClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        for attempt in range(5):
            response = await self._client.get(path, params=params)
            if response.status_code in (418, 429) or response.status_code >= 500:
                await asyncio.sleep(min(60.0, 2.0**attempt))
                continue
            if response.status_code != 200:
                raise BinanceError(f"{path}: HTTP {response.status_code} {response.text[:200]}")
            await asyncio.sleep(self._pause)
            return response.json()
        raise BinanceError(f"{path}: биржа недоступна после повторов")

    async def _klines(
        self,
        path: str,
        start: datetime,
        end: datetime,
        *,
        symbol_key: str = "symbol",
        interval: str = "1h",
    ) -> list[list[Any]]:
        step_ms = int(INTERVAL_STEPS[interval].total_seconds() * 1000)
        rows: list[list[Any]] = []
        cursor = datetime_to_ms(start)
        end_ms = datetime_to_ms(end)
        while cursor < end_ms:
            page = await self._get(
                path,
                {
                    symbol_key: SYMBOL,
                    "interval": interval,
                    "startTime": cursor,
                    "endTime": end_ms - 1,
                    "limit": KLINES_LIMIT,
                },
            )
            if not page:
                break
            rows.extend(page)
            next_cursor = int(page[-1][0]) + step_ms
            if next_cursor <= cursor:
                raise BinanceError(f"{path}: пагинация не продвигается")
            cursor = next_cursor
        return rows

    async def candles(
        self, start: datetime, end: datetime, *, now: datetime, interval: str = "1h"
    ) -> list[Candle]:
        """Закрытые свечи с open_time в [start, end). В raw — объём покупок агрессором
        (taker buy base volume): исторический аналог потока сделок."""
        result = []
        for k in await self._klines("/fapi/v1/klines", start, end, interval=interval):
            open_time = ms_to_datetime(int(k[0]))
            close_time = ms_to_datetime(int(k[6])) + timedelta(milliseconds=1)
            if close_time > now:
                continue  # незакрытая свеча
            result.append(
                Candle(
                    interval=interval,
                    open_time=open_time,
                    close_time=close_time,
                    open=Decimal(k[1]),
                    high=Decimal(k[2]),
                    low=Decimal(k[3]),
                    close=Decimal(k[4]),
                    volume=Decimal(k[5]),
                    trades_count=int(k[8]),
                    is_closed=True,
                    source=DataSource.IMPORT,
                    received_at=now,
                    raw={"taker_buy": k[9]},
                )
            )
        return result

    async def funding_rates(self, start: datetime, end: datetime) -> list[tuple[datetime, Decimal]]:
        """Рассчитанные ставки funding (раз в 8 ч): [(fundingTime, rate)]."""
        result: dict[int, Decimal] = {}
        cursor = datetime_to_ms(start)
        end_ms = datetime_to_ms(end)
        while cursor < end_ms:
            page = await self._get(
                "/fapi/v1/fundingRate",
                {"symbol": SYMBOL, "startTime": cursor, "endTime": end_ms, "limit": FUNDING_LIMIT},
            )
            if not page:
                break
            for item in page:
                result[int(item["fundingTime"])] = Decimal(item["fundingRate"])
            last = max(int(i["fundingTime"]) for i in page)
            if len(page) < FUNDING_LIMIT:
                break
            if last + 1 <= cursor:
                raise BinanceError("fundingRate: пагинация не продвигается")
            cursor = last + 1
        return [(ms_to_datetime(t), r) for t, r in sorted(result.items())]

    async def premium(self, start: datetime, end: datetime) -> list[tuple[datetime, Decimal]]:
        """Premium index на закрытии каждого часа: [(конец часа, premium)]."""
        rows = await self._klines("/fapi/v1/premiumIndexKlines", start, end)
        return [(ms_to_datetime(int(k[0])) + HOUR, Decimal(k[4])) for k in rows]


def hourly_funding(
    rates: Sequence[tuple[datetime, Decimal]], premium: Sequence[tuple[datetime, Decimal]]
) -> list[FundingPoint]:
    """Почасовые точки в масштабе Hyperliquid: ставка Binance / 8 и premium на конец часа.
    Для часа берётся последняя ставка, рассчитанная не позже конца этого часа."""
    times = [t for t, _ in rates]
    points = []
    for end_of_hour, prem in premium:
        i = bisect.bisect_right(times, end_of_hour) - 1
        if i < 0:
            continue  # до первой известной ставки
        if end_of_hour - times[i] > timedelta(hours=FUNDING_PERIOD_HOURS + 1):
            continue  # пропуск в истории ставок — не выдумываем значение
        points.append(
            FundingPoint(
                time=end_of_hour,
                funding_rate=rates[i][1] / FUNDING_PERIOD_HOURS,
                premium=prem,
            )
        )
    return points


def utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
