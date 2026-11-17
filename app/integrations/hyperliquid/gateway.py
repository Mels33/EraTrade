"""HyperliquidGateway — единственная точка доступа к бирже для остальной системы.

Этап 3: только чтение. Методы place_order / cancel_order появятся на этапе 6.
Наружу отдаются только доменные типы, структуры API и SDK не протекают.
"""

import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config.settings import Network, Settings
from app.domain.account import (
    AccountState,
    ActiveAssetData,
    AgentInfo,
    BestBidAsk,
    Fill,
    FundingEvent,
    LedgerEvent,
    OpenOrder,
    OrderStatusResult,
    RateLimitStatus,
    UserRole,
)
from app.domain.market import AssetContext, Candle, MarketMeta
from app.integrations.hyperliquid import parsing
from app.integrations.hyperliquid.errors import HyperliquidFormatError, UnknownMarketError
from app.integrations.hyperliquid.info_client import InfoClient

INTERVALS: dict[str, timedelta] = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "1h": timedelta(hours=1),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}
FILLS_PAGE_LIMIT = 2000  # userFillsByTime отдаёт не больше 2000 исполнений за запрос
EVENTS_PAGE_LIMIT = 500  # userFunding / userNonFundingLedgerUpdates
DEFAULT_CLOSE_GRACE = timedelta(seconds=10)

_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CLOID_RE = re.compile(r"^0x[0-9a-fA-F]{32}$")


def normalize_address(address: str) -> str:
    if not _ADDRESS_RE.match(address):
        raise ValueError("адрес должен иметь вид 0x + 40 hex-символов")
    return address.lower()


def _utc_now() -> datetime:
    return datetime.now(UTC)


class HyperliquidGateway:
    def __init__(
        self,
        client: InfoClient,
        network: Network,
        *,
        clock: Callable[[], datetime] = _utc_now,
        close_grace: timedelta = DEFAULT_CLOSE_GRACE,
    ) -> None:
        self._client = client
        self.network = network
        self._clock = clock
        self._close_grace = close_grace

    # --- рынок ---

    async def get_markets(self) -> list[MarketMeta]:
        data = await self._client.post({"type": "meta"})
        return parsing.parse_meta(data, self.network, self._clock())

    async def get_market(self, coin: str) -> MarketMeta:
        for market in await self.get_markets():
            if market.coin == coin:
                return market
        raise UnknownMarketError(f"инструмента {coin} нет в meta сети {self.network}")

    async def get_markets_with_contexts(
        self,
    ) -> tuple[list[MarketMeta], dict[str, AssetContext]]:
        data = await self._client.post({"type": "metaAndAssetCtxs"})
        return parsing.parse_meta_and_contexts(data, self.network, self._clock())

    async def get_all_mids(self) -> dict[str, Decimal]:
        return parsing.parse_all_mids(await self._client.post({"type": "allMids"}))

    async def get_candles(
        self, coin: str, interval: str, start: datetime, end: datetime | None = None
    ) -> list[Candle]:
        """Свечи с open_time в [start, end), с постраничной догрузкой. Биржа хранит
        только ~5000 последних свечей на интервал — более ранние данные не вернутся."""
        step = INTERVALS.get(interval)
        if step is None:
            raise ValueError(f"неподдерживаемый интервал {interval}")
        end = end or self._clock()
        result: dict[datetime, Candle] = {}
        cursor = start
        while cursor < end:
            data = await self._client.post(
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": coin,
                        "interval": interval,
                        "startTime": parsing.datetime_to_ms(cursor),
                        "endTime": parsing.datetime_to_ms(end),
                    },
                }
            )
            page = parsing.parse_candle_snapshot(
                data,
                coin=coin,
                interval=interval,
                received_at=self._clock(),
                close_grace=self._close_grace,
            )
            page = [c for c in page if start <= c.open_time < end]
            if not page:
                break
            for candle in page:
                result[candle.open_time] = candle
            next_cursor = page[-1].open_time + step
            if next_cursor <= cursor:
                raise HyperliquidFormatError("candleSnapshot: пагинация не продвигается")
            cursor = next_cursor
        return [result[t] for t in sorted(result)]

    # --- аккаунт (адрес основного аккаунта или субаккаунта, НЕ API-кошелька) ---

    async def get_account_state(self, user: str) -> AccountState:
        data = await self._client.post(
            {"type": "clearinghouseState", "user": normalize_address(user)}
        )
        return parsing.parse_clearinghouse_state(data)

    async def get_open_orders(self, user: str) -> list[OpenOrder]:
        data = await self._client.post(
            {"type": "frontendOpenOrders", "user": normalize_address(user)}
        )
        return parsing.parse_open_orders(data)

    async def get_fills(
        self, user: str, start: datetime, end: datetime | None = None
    ) -> list[Fill]:
        """Исполнения за [start, end] с постраничной догрузкой и дедупликацией по tid."""
        address = normalize_address(user)
        result: dict[int, Fill] = {}
        cursor = start
        while True:
            payload: dict[str, object] = {
                "type": "userFillsByTime",
                "user": address,
                "startTime": parsing.datetime_to_ms(cursor),
            }
            if end is not None:
                payload["endTime"] = parsing.datetime_to_ms(end)
            page = parsing.parse_fills(await self._client.post(payload))
            for fill in page:
                result[fill.tid] = fill
            if len(page) < FILLS_PAGE_LIMIT:
                break
            last_time = max(f.time for f in page)
            if last_time <= cursor:
                # Больше FILLS_PAGE_LIMIT исполнений в одну миллисекунду — продвинуться нельзя.
                raise HyperliquidFormatError("userFillsByTime: пагинация не продвигается")
            cursor = last_time  # не +1 мс: соседние fills в ту же мс отсекутся дедупликацией
        return sorted(result.values(), key=lambda f: (f.time, f.tid))

    async def get_order_status(self, user: str, order_id: int | str) -> OrderStatusResult:
        """order_id — числовой oid биржи или cloid вида 0x + 32 hex."""
        if isinstance(order_id, str) and not _CLOID_RE.match(order_id):
            raise ValueError("cloid должен иметь вид 0x + 32 hex-символа")
        data = await self._client.post(
            {"type": "orderStatus", "user": normalize_address(user), "oid": order_id}
        )
        return parsing.parse_order_status(data)

    async def get_user_rate_limit(self, user: str) -> RateLimitStatus:
        data = await self._client.post({"type": "userRateLimit", "user": normalize_address(user)})
        return parsing.parse_user_rate_limit(data)

    async def _events_by_time[E](
        self,
        request_type: str,
        user: str,
        start: datetime,
        parse: Callable[[object], list[E]],
        key: Callable[[E], tuple[str, str]],
        time_of: Callable[[E], datetime],
    ) -> list[E]:
        address = normalize_address(user)
        result: dict[tuple[str, str], E] = {}
        cursor = start
        while True:
            page = parse(
                await self._client.post(
                    {
                        "type": request_type,
                        "user": address,
                        "startTime": parsing.datetime_to_ms(cursor),
                    }
                )
            )
            for event in page:
                result[key(event)] = event
            if len(page) < EVENTS_PAGE_LIMIT:
                break
            last = max(time_of(e) for e in page)
            if last <= cursor:
                raise HyperliquidFormatError(f"{request_type}: пагинация не продвигается")
            cursor = last
        return sorted(result.values(), key=time_of)

    async def get_funding(self, user: str, start: datetime) -> list[FundingEvent]:
        return await self._events_by_time(
            "userFunding",
            user,
            start,
            parsing.parse_funding_events,
            key=lambda e: (e.hash, e.coin),
            time_of=lambda e: e.time,
        )

    async def get_ledger(self, user: str, start: datetime) -> list[LedgerEvent]:
        return await self._events_by_time(
            "userNonFundingLedgerUpdates",
            user,
            start,
            parsing.parse_ledger_events,
            key=lambda e: (e.hash, e.delta_type),
            time_of=lambda e: e.time,
        )

    async def get_active_asset_data(self, user: str, coin: str) -> ActiveAssetData:
        data = await self._client.post(
            {"type": "activeAssetData", "user": normalize_address(user), "coin": coin}
        )
        return parsing.parse_active_asset_data(data)

    async def get_best_bid_ask(self, coin: str) -> BestBidAsk:
        return parsing.parse_best_bid_ask(await self._client.post({"type": "l2Book", "coin": coin}))

    async def get_user_role(self, address: str) -> UserRole:
        data = await self._client.post({"type": "userRole", "user": normalize_address(address)})
        return parsing.parse_user_role(data)

    async def get_extra_agents(self, user: str) -> list[AgentInfo]:
        data = await self._client.post({"type": "extraAgents", "user": normalize_address(user)})
        return parsing.parse_extra_agents(data)


@asynccontextmanager
async def open_gateway(settings: Settings) -> AsyncIterator[HyperliquidGateway]:
    """Шлюз для сети из настроек: URL выводится из HYPERLIQUID_NETWORK, вручную не задаётся."""
    async with InfoClient(settings.hyperliquid_api_url) as client:
        yield HyperliquidGateway(client, settings.network)
