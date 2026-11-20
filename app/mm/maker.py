"""Живой маркет-мейкер: post-only (ALO) котировки на монетах из [mm].

Источник истины — биржа. По каждой монете у бота не больше одной заявки на сторону; сверка
(каждые sync_seconds) берёт позицию и открытые заявки с биржи и приводит локальное состояние
к ним: лишние заявки на монетах [mm] отменяются. Повторной отправки нет: при неизвестном
исходе действия заявка помечается «уточняется» и разбирается следующей сверкой.

Защита:
- котирует только при режиме LIVE_ENABLED и без запрета входов (пауза/kill из панели снимают
  котировки; DISABLED — ещё и закрывает позицию);
- предел позиции на монету, дневной убыток (реализованный + нереализованный) → снять всё,
  закрыть позиции, запретить входы до решения оператора;
- scheduleCancel: если процесс завис или упал, биржа сама снимет заявки;
- стакан не обновлялся stale_book_seconds — котировки снимаются;
- бюджет действий в минуту и резерв лимита запросов адреса.
"""

import asyncio
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import DeadMansSwitchConfig, MmConfig
from app.db.repositories import control
from app.db.repositories import mm as mm_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.domain.instruments import InstrumentRules, Rounding
from app.domain.orders import PlacementFormatError, PlacementKind, parse_placement_status
from app.execution.dead_mans_switch import DeadMansSwitch, DeadMansSwitchError
from app.execution.session import TradingSession
from app.integrations.hyperliquid import actions, parsing
from app.integrations.hyperliquid.errors import (
    ExchangeNotSentError,
    ExchangeOutcomeUnknownError,
    HyperliquidError,
)
from app.integrations.hyperliquid.exchange_client import ExchangeResponse
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.ws import (
    Connector,
    default_connector,
    iter_messages,
    subscribe_message,
)
from app.market_data.flow import BookSnapshot
from app.mm.adverse import load_adverse_selection
from app.mm.quoter import MIN_ORDER_USD, FlowWindow, Quote, QuoteDecision, decide
from app.mm.scanner import MarketScan, choose_coins, scan, score
from app.observability.notify import LogNotifier, Notifier

COMPONENT = "mm"
MM_ACTOR = Actor(ActorKind.SYSTEM, "mm")
OPEN_ORDERS_EVERY = 2  # открытые заявки — каждую вторую сверку (вес запроса 20)
FILLS_BACKFILL_SECONDS = 60
RATE_LIMIT_CHECK_SECONDS = 300
FLATTEN_SLIPPAGE = Decimal("0.01")
FLATTEN_ATTEMPTS = 3

ScanFn = Callable[[], Awaitable[list[MarketScan]]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class Slot:
    """Наша заявка на одной стороне."""

    cloid: str
    price: Decimal
    size: Decimal
    oid: int | None = None
    pending: bool = True  # исход последнего действия ещё не подтверждён биржей
    since: float = 0.0  # монотонное время последнего действия


@dataclass
class CoinState:
    rules: InstrumentRules
    flow: FlowWindow
    book: BookSnapshot | None = None
    book_at: float = 0.0
    position: Decimal = Decimal(0)
    unrealized: Decimal = Decimal(0)
    slots: dict[Side, Slot | None] = field(
        default_factory=lambda: {Side.BUY: None, Side.SELL: None}
    )
    last_action: dict[Side, float] = field(default_factory=lambda: {Side.BUY: 0.0, Side.SELL: 0.0})
    decision: QuoteDecision | None = None
    retiring: bool = False  # уходим с монеты: только закрытие позиции
    retire_since: float = 0.0
    flatten_attempts: int = 0


class ActionBudget:
    """Не больше limit единиц (заявка/отмена/перестановка) за скользящую минуту."""

    def __init__(self, limit: int, clock: Callable[[], float]) -> None:
        self.limit = limit
        self.clock = clock
        self._events: deque[tuple[float, int]] = deque()

    def used(self) -> int:
        now = self.clock()
        while self._events and self._events[0][0] < now - 60:
            self._events.popleft()
        return sum(n for _, n in self._events)

    def allows(self, units: int) -> bool:
        return self.used() + units <= self.limit

    def spend(self, units: int) -> None:
        self._events.append((self.clock(), units))


class MarketMaker:
    def __init__(
        self,
        session: TradingSession,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        cfg: MmConfig,
        *,
        network: Network,
        user: str,
        ws_url: str,
        rules: dict[str, InstrumentRules],
        scanner: ScanFn | None = None,
        notifier: Notifier | None = None,
        connector: Connector = default_connector,
        now: Callable[[], datetime] = _utc_now,
        mono: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.sm = sessionmaker
        self.cfg = cfg
        self.network = network
        self.user = user
        self.ws_url = ws_url
        self.notifier = notifier or LogNotifier()
        self.connector = connector
        self.now = now
        self.mono = mono
        window = timedelta(seconds=cfg.flow_window_seconds)
        self._window = window
        self.coins = {c: CoinState(rules=r, flow=FlowWindow(window)) for c, r in rules.items()}
        self.known: set[str] = set(self.coins)  # все монеты этого запуска — для учёта исполнений
        self.scanner: ScanFn = scanner or (
            lambda: scan(
                gateway,
                top=cfg.select_scan_top,
                samples=4,
                pause_s=3,
                min_volume_usd=cfg.select_min_volume_usd,
                adverse_selection=load_adverse_selection(Path(cfg.adverse_selection_path)),
            )
        )
        self.selection: dict[str, Any] = {}
        self._next_select_at: datetime | None = None
        self._resubscribe = False
        self.budget = ActionBudget(cfg.max_actions_per_minute, mono)
        self.instance_id = uuid.uuid4().hex[:12]
        self.started_at = now()
        self.allowed = False  # режим и запрет входов из БД
        self.mode: str | None = None
        self.halt_reason: str | None = None  # стоп до перезапуска (лимит убытка и т.п.)
        self.pause_reason: str | None = None  # временная пауза (лимит запросов)
        self.account_value: Decimal | None = None
        self.realized_today = Decimal(0)
        self.requests_remaining: int | None = None
        self._act = asyncio.Lock()
        self._sync_now = asyncio.Event()
        self._syncs = 0
        self._last_backfill = 0.0
        self._last_rate_check = -1e9
        self._flattened_for_disabled = False
        self._ws_connected = False
        self.dms_error: str | None = None
        self.spot_usdc: Decimal | None = None

    # --- запуск ---

    async def startup(self) -> None:
        """Подхватить монеты прошлого запуска с позицией, выбрать монеты, снять старые заявки."""
        await self._adopt_previous()
        if self.cfg.auto_select:
            try:
                await self.select()
            except Exception as exc:  # без сканера работаем на стартовых монетах
                logfire.error("mm: выбор монет не удался: {e}", e=repr(exc))
        await self.sync(force_orders=True)
        stale = [(c, s) for c, st in self.coins.items() for s in Side if st.slots[s]]
        if stale:
            logfire.warn("mm: снимаю заявки прошлого запуска: {n}", n=len(stale))
            await self.cancel_all()
        for coin, st in list(self.coins.items()):
            if st.position == 0 and not st.retiring:
                await self._set_leverage(coin)

    async def _adopt_previous(self) -> None:
        """Монеты прошлого запуска (heartbeat в БД), по которым осталась позиция, — под контроль:
        иначе позиция на монете, выбранной автоматически, осталась бы без присмотра."""
        async with self.sm() as db:
            beat = next(
                (h for h in await control.get_heartbeats(db) if h.component == COMPONENT), None
            )
        previous = list((beat.details.get("coins") or {}).keys()) if beat else []
        missing = [c for c in previous if c not in self.coins]
        if not missing:
            return
        account = await self.gateway.get_account_state(self.user)
        for coin in missing:
            if (pos := account.position(coin)) is not None and pos.size != 0:
                logfire.warn(
                    "mm: подхватываю {coin} с позицией {size}", coin=coin, size=str(pos.size)
                )
                await self.add_coin(coin, retiring=True)

    # --- выбор монет ---

    async def add_coin(self, coin: str, *, retiring: bool = False) -> None:
        if coin in self.coins:
            st = self.coins[coin]
            if st.retiring and not retiring:
                st.retiring, st.flatten_attempts = False, 0
            return
        markets = {m.coin: m for m in await self.gateway.get_markets()}
        if coin not in markets:
            logfire.warn("mm: рынка {coin} нет", coin=coin)
            return
        self.known.add(coin)
        self.coins[coin] = CoinState(
            rules=InstrumentRules.for_perp(markets[coin]),
            flow=FlowWindow(self._window),
            retiring=retiring,
            retire_since=self.mono(),
        )
        self._resubscribe = True
        if not retiring:
            await self._set_leverage(coin)

    def retire(self, coin: str) -> None:
        st = self.coins.get(coin)
        if st is not None and not st.retiring:
            st.retiring, st.retire_since, st.flatten_attempts = True, self.mono(), 0
            logfire.info("mm: ухожу с {coin}", coin=coin)

    async def select(self) -> list[str]:
        """Пересмотр монет сканером: добавить лучшие, уйти с вытесненных."""
        scans = await self.scanner()
        active = [c for c, st in self.coins.items() if not st.retiring]
        target, ranked = choose_coins(
            scans,
            active,
            max_coins=self.cfg.max_coins,
            min_spread_bps=self.cfg.select_min_spread_bps,
            max_spread_bps=self.cfg.select_max_spread_bps,
            min_volume_usd=self.cfg.select_min_volume_usd,
            min_leverage=self.cfg.leverage,
            exclude=self.cfg.exclude,
            hysteresis=self.cfg.select_hysteresis,
            min_top_depth_usd=self.cfg.select_min_top_depth_usd,
        )
        for coin in target:
            await self.add_coin(coin)
        for coin in active:
            if coin not in target:
                self.retire(coin)
        now = self.now()
        self._next_select_at = now + timedelta(minutes=self.cfg.rescan_minutes)
        self.selection = {
            "at": now.isoformat(),
            "next_at": self._next_select_at.isoformat(),
            "target": target,
            "candidates": [
                {
                    "coin": c.coin,
                    "spread_bps": round(float(c.spread_bps), 2),
                    "tick_bps": round(float(c.tick_bps), 2),
                    "adverse_selection_bps": round(float(c.adverse_selection_bps), 2),
                    "edge_bps": round(float(c.edge_bps), 2),
                    "volume_usd": float(c.day_volume_usd),
                    "score": round(score(c)),
                }
                for c in ranked[:8]
            ],
        }
        logfire.info("mm: выбор монет {target}", target=target)
        return target

    async def _select_forever(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.rescan_minutes * 60)
            try:
                await self.select()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logfire.error("mm: выбор монет не удался: {e}", e=repr(exc))

    async def _set_leverage(self, coin: str) -> None:
        st = self.coins[coin]
        try:
            response = await self.session.submit(
                actions.update_leverage_action(
                    st.rules.asset_id, is_cross=False, leverage=self.cfg.leverage
                )
            )
        except HyperliquidError as exc:
            logfire.warn("mm: плечо {coin} не установлено: {e}", coin=coin, e=str(exc))
            return
        if not response.ok:
            logfire.warn("mm: плечо {coin}: {e}", coin=coin, e=response.error)

    async def run(self) -> None:
        dms = DeadMansSwitch(
            self.session,
            DeadMansSwitchConfig(
                enabled=True,
                horizon_seconds=self.cfg.dms_horizon_seconds,
                refresh_seconds=self.cfg.dms_refresh_seconds,
            ),
        )
        await self.startup()
        logfire.info("mm: запущен, монеты {coins}", coins=list(self.coins))
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._dms_forever(dms))
                tg.create_task(self._ws_forever())
                tg.create_task(self._sync_forever())
                tg.create_task(self._quote_forever())
                if self.cfg.auto_select:
                    tg.create_task(self._select_forever())
        finally:
            await asyncio.shield(self._shutdown())

    async def _dms_forever(self, dms: DeadMansSwitch) -> None:
        try:
            await dms.run()
        except DeadMansSwitchError as exc:
            self.dms_error = str(exc)
            logfire.error("mm: scheduleCancel недоступен, работаю без него: {e}", e=str(exc))

    async def _shutdown(self) -> None:
        try:
            await self.cancel_all()
        except Exception as exc:  # биржа снимет заявки сама по scheduleCancel
            logfire.error("mm: при остановке заявки не сняты: {e}", e=repr(exc))

    # --- рыночные данные ---

    async def _ws_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                conn = await self.connector(self.ws_url)
                try:
                    await self._ws_session(conn)
                finally:
                    self._ws_connected = False
                    await conn.close()
                delay = 1.0  # штатная переподписка (сменились монеты)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logfire.warn(
                    "mm: WebSocket: {e}, переподключение через {d} с", e=repr(exc), d=delay
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def _ws_session(self, conn: Any) -> None:
        self._resubscribe = False
        for coin in list(self.coins):
            await conn.send(subscribe_message({"type": "l2Book", "coin": coin}))
            await conn.send(subscribe_message({"type": "trades", "coin": coin}))
        await conn.send(subscribe_message({"type": "userFills", "user": self.user}))
        self._ws_connected = True
        async for channel, data in iter_messages(conn, silence_timeout=30, ping_interval=20):
            await self.handle(channel, data)
            if self._resubscribe:
                return

    async def handle(self, channel: str, data: Any) -> None:
        if channel == "l2Book":
            coin = data.get("coin") if isinstance(data, dict) else None
            if coin in self.coins:
                book = parsing.parse_ws_l2book(data, coin=coin)
                if book is not None:
                    st = self.coins[coin]
                    st.book, st.book_at = book, self.mono()
        elif channel == "trades":
            items = data if isinstance(data, list) else []
            coin = items[0].get("coin") if items and isinstance(items[0], dict) else None
            if coin in self.coins:
                for trade in parsing.parse_ws_trades(data, coin=coin):
                    self.coins[coin].flow.add(trade)
        elif channel == "userFills" and isinstance(data, dict):
            fills = [parsing.parse_fill(f) for f in data.get("fills") or []]
            ours = [f for f in fills if f.coin in self.known]
            if not ours:
                return
            async with self.sm.begin() as db:
                added = await mm_repo.add_fills(db, ours)
            if data.get("isSnapshot"):
                return
            for f in ours:
                if (
                    f.tid not in added or f.coin not in self.coins
                ):  # уже учтено (повтор WS или догрузка через REST)
                    continue
                st = self.coins[f.coin]
                st.position += f.size if f.side is Side.BUY else -f.size
                slot = st.slots[f.side]
                if slot is not None and (slot.oid == f.oid or slot.cloid == f.cloid):
                    slot.size -= f.size
                    if slot.size <= 0:
                        st.slots[f.side] = None
            if added:
                logfire.info("mm: исполнений {n}", n=len(added))
                self._sync_now.set()

    # --- сверка с биржей ---

    async def _sync_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logfire.error("mm: сверка не удалась: {e}", e=repr(exc))
            self._sync_now.clear()
            try:
                await asyncio.wait_for(self._sync_now.wait(), timeout=self.cfg.sync_seconds)
            except TimeoutError:
                pass

    async def sync(self, *, force_orders: bool = False) -> None:
        self._syncs += 1
        async with self.sm() as db:
            state = await control.get_bot_state(db)
        self.mode = state.trading_mode if state else None
        blocked = bool(state and state.entries_blocked)
        self.allowed = self.mode == TradingMode.LIVE_ENABLED.value and not blocked

        account = await self.gateway.get_account_state(self.user)
        self.account_value = account.account_value
        for coin, st in list(self.coins.items()):
            pos = account.position(coin)
            st.position = pos.size if pos else Decimal(0)
            st.unrealized = pos.unrealized_pnl if pos else Decimal(0)

        any_pending = any(s and s.pending for st in self.coins.values() for s in st.slots.values())
        if force_orders or any_pending or self._syncs % OPEN_ORDERS_EVERY == 0:
            await self._sync_orders()

        mono = self.mono()
        if mono - self._last_backfill >= FILLS_BACKFILL_SECONDS:
            self._last_backfill = mono
            await self._backfill_fills()
        if mono - self._last_rate_check >= RATE_LIMIT_CHECK_SECONDS:
            self._last_rate_check = mono
            # Единый счёт: залог лежит на споте, perp accountValue может быть 0.
            self.spot_usdc = await self.gateway.get_spot_usdc(self.user)
            limit = await self.gateway.get_user_rate_limit(self.user)
            self.requests_remaining = limit.remaining
            self.pause_reason = (
                f"мало запросов адреса: {limit.remaining}"
                if limit.remaining < self.cfg.min_requests_remaining
                else None
            )

        await self._retire_housekeeping()

        day_start = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.sm() as db:
            self.realized_today = await mm_repo.realized_since(db, day_start)
        pnl = self.realized_today + sum((st.unrealized for st in self.coins.values()), Decimal(0))
        if self.halt_reason is None and pnl <= -self.cfg.daily_loss_usd:
            await self.halt(f"маркет-мейкер: дневной убыток {pnl:.2f} USDC", flatten=True)

        if self.mode == TradingMode.DISABLED.value:
            if not self._flattened_for_disabled:
                self._flattened_for_disabled = True
                await self.cancel_all()
                await self.flatten_all()
        else:
            self._flattened_for_disabled = False
        await self._beat(pnl)

    async def _retire_housekeeping(self) -> None:
        """Монета, с которой уходим: позиция закрыта и заявок нет → убрать; не закрылась за
        retire_flatten_minutes (или остаток меньше минимальной заявки) → закрыть по рынку."""
        for coin, st in list(self.coins.items()):
            if not st.retiring:
                continue
            busy = any(st.slots[s] is not None for s in Side)
            if st.position == 0 and not busy:
                del self.coins[coin]
                self._resubscribe = True
                logfire.info("mm: {coin} закрыт и убран", coin=coin)
                continue
            if st.position == 0:
                continue
            mid = st.decision.mid if st.decision and st.decision.mid else None
            dust = mid is not None and abs(st.position) * mid < MIN_ORDER_USD
            timeout = self.mono() - st.retire_since > self.cfg.retire_flatten_minutes * 60
            if not (dust or timeout):
                continue
            if st.flatten_attempts >= FLATTEN_ATTEMPTS:
                if dust:  # биржа не принимает такой маленький остаток — оставляем
                    logfire.warn(
                        "mm: остаток {coin} {size} меньше минимума, оставлен",
                        coin=coin,
                        size=str(st.position),
                    )
                    del self.coins[coin]
                    self._resubscribe = True
                continue
            st.flatten_attempts += 1
            async with self._act:
                sides = [s for s in Side if st.slots[s] is not None]
                if sides:
                    await self._cancel(coin, sides)
                await self._flatten(coin)

    async def _sync_orders(self) -> None:
        orders = await self.gateway.get_open_orders(self.user)
        extra: list[tuple[int, int]] = []
        for coin, st in list(self.coins.items()):
            for side in Side:
                mine = [o for o in orders if o.coin == coin and o.side is side and not o.is_trigger]
                slot = st.slots[side]
                keep = next(
                    (o for o in mine if slot and (o.oid == slot.oid or o.cloid == slot.cloid)),
                    None,
                )
                if keep is None and mine:
                    keep = max(mine, key=lambda o: o.created_at)
                for o in mine:
                    if o is not keep:
                        extra.append((st.rules.asset_id, o.oid))
                if keep is None:
                    # Не на бирже: исполнена или отменена. Только что отправленную заявку
                    # не трогаем пару секунд — ответ мог обогнать openOrders.
                    if slot is not None and (not slot.pending or self.mono() - slot.since > 5):
                        st.slots[side] = None
                else:
                    st.slots[side] = Slot(
                        cloid=keep.cloid or (slot.cloid if slot else actions.new_cloid()),
                        price=keep.limit_price,
                        size=keep.size,
                        oid=keep.oid,
                        pending=False,
                        since=slot.since if slot else self.mono(),
                    )
        if extra:
            logfire.warn("mm: лишние заявки на бирже, отменяю: {n}", n=len(extra))
            async with self._act:
                await self._submit(actions.cancel_many_action(extra), units=len(extra))

    async def _backfill_fills(self) -> None:
        async with self.sm() as db:
            last = await mm_repo.last_fill_time(db)
        since = (last - timedelta(minutes=5)) if last else self.now() - timedelta(days=1)
        fills = [f for f in await self.gateway.get_fills(self.user, since) if f.coin in self.known]
        async with self.sm.begin() as db:
            added = await mm_repo.add_fills(db, fills)
        if added:
            logfire.info("mm: догружено исполнений {n}", n=len(added))

    async def _beat(self, pnl: Decimal) -> None:
        coins = {}
        for coin, st in list(self.coins.items()):
            d = st.decision
            coins[coin] = {
                "mid": str(d.mid) if d and d.mid else None,
                "spread_bps": float(d.spread_bps) if d and d.spread_bps is not None else None,
                "bid": _slot_json(st.slots[Side.BUY]),
                "ask": _slot_json(st.slots[Side.SELL]),
                "position": str(st.position),
                "position_usd": str((st.position * d.mid).quantize(Decimal("0.01")))
                if d and d.mid
                else None,
                "unrealized": str(st.unrealized),
                "flow": st.flow.imbalance(self.now(), Decimal(0)),
                "reasons": list(d.reasons) if d else [],
                "book_age_s": round(self.mono() - st.book_at, 1) if st.book_at else None,
                "retiring": st.retiring,
            }
        details = {
            "state": self.status_text(),
            "quoting": self.quoting_allowed(),
            "pnl_today": str(pnl.quantize(Decimal("0.0001"))),
            "realized_today": str(self.realized_today.quantize(Decimal("0.0001"))),
            "daily_loss_limit": str(self.cfg.daily_loss_usd),
            "account_value": str(self.account_value) if self.account_value is not None else None,
            "spot_usdc": str(self.spot_usdc) if self.spot_usdc is not None else None,
            "actions_last_min": self.budget.used(),
            "actions_limit": self.cfg.max_actions_per_minute,
            "requests_remaining": self.requests_remaining,
            "ws": self._ws_connected,
            "dms_error": self.dms_error,
            "auto_select": self.cfg.auto_select,
            "selection": self.selection,
            "coins": coins,
        }
        async with self.sm.begin() as db:
            await control.beat(
                db,
                component=COMPONENT,
                instance_id=self.instance_id,
                started_at=self.started_at,
                now=self.now(),
                details=details,
            )

    # --- решения ---

    def quoting_allowed(self) -> bool:
        return self.allowed and self.halt_reason is None and self.pause_reason is None

    def status_text(self) -> str:
        if self.halt_reason:
            return f"остановлен: {self.halt_reason}"
        if not self.allowed:
            return f"не котирует: режим {self.mode}" + (
                "" if self.mode != TradingMode.LIVE_ENABLED.value else ", запрет входов"
            )
        if self.pause_reason:
            return f"пауза: {self.pause_reason}"
        return "котирует"

    async def _quote_forever(self) -> None:
        while True:
            try:
                await self.quote_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logfire.error("mm: ошибка котирования: {e}", e=repr(exc))
                await asyncio.sleep(2)
            await asyncio.sleep(0.5)

    async def quote_once(self) -> None:
        for coin, st in list(self.coins.items()):
            book = st.book
            if book is None or self.mono() - st.book_at > self.cfg.stale_book_seconds:
                st.decision = QuoteDecision(None, None, reasons=("стакан устарел",))
                await self._apply(coin, {Side.BUY: None, Side.SELL: None})
                continue
            own = {s: (Quote(sl.price, sl.size) if (sl := st.slots[s]) else None) for s in Side}
            # Решение считается и без права котировать — для панели (цена, спред, причины).
            st.decision = decide(
                book,
                st.rules,
                self.cfg,
                position=st.position,
                own_bid=own[Side.BUY],
                own_ask=own[Side.SELL],
                imbalance=st.flow.imbalance(self.now(), self.cfg.flow_min_usd),
                reduce_only=st.retiring,
            )
            if not self.quoting_allowed():
                await self._apply(coin, {Side.BUY: None, Side.SELL: None})
                continue
            await self._apply(coin, {Side.BUY: st.decision.bid, Side.SELL: st.decision.ask})

    async def _apply(self, coin: str, want: dict[Side, Quote | None]) -> None:
        st = self.coins[coin]
        place: list[tuple[Side, Quote]] = []
        modify: list[tuple[Side, Quote]] = []
        cancel: list[Side] = []
        now = self.mono()
        for side in Side:
            slot, quote = st.slots[side], want[side]
            if slot is not None and slot.pending:
                continue  # ждём подтверждения биржей
            if quote is None:
                if slot is not None:
                    cancel.append(side)
            elif slot is None:
                place.append((side, quote))
            elif quote.reduce_only and slot.size > quote.size:
                modify.append((side, quote))  # заявка больше нужного (закрытие позиции) — сразу
            elif abs(slot.price - quote.price) > max(
                st.rules.tick(quote.price), quote.price * self.cfg.requote_slack_bps / 10_000
            ):
                # Отойти от рынка — сразу (иначе исполнят по устаревшей цене); подтянуться
                # к лучшей цене — не чаще requote_min_interval_seconds.
                retreat = quote.price < slot.price if side is Side.BUY else quote.price > slot.price
                interval_ok = now - st.last_action[side] >= self.cfg.requote_min_interval_seconds
                if retreat or interval_ok:
                    modify.append((side, quote))
        if not (place or modify or cancel):
            return
        async with self._act:
            if cancel:
                await self._cancel(coin, cancel)
            if modify and self.budget.allows(len(modify)):
                await self._modify(coin, modify)
            if place and self.budget.allows(len(place)):
                await self._place(coin, place)

    # --- действия на бирже (под self._act) ---

    def _request(self, coin: str, side: Side, quote: Quote, cloid: str) -> actions.OrderRequest:
        return actions.OrderRequest(
            asset=self.coins[coin].rules.asset_id,
            is_buy=side is Side.BUY,
            price=quote.price,
            size=quote.size,
            reduce_only=quote.reduce_only,
            order_type=actions.LimitOrderType(actions.Tif.ALO),
            cloid=cloid,
        )

    async def _submit(self, action: dict[str, Any], *, units: int) -> ExchangeResponse | None:
        """None — исход неизвестен (нужна сверка); ExchangeNotSentError пробрасывается."""
        self.budget.spend(units)
        try:
            return await self.session.submit(action)
        except ExchangeOutcomeUnknownError as exc:
            logfire.warn("mm: исход {t} неизвестен: {e}", t=action["type"], e=str(exc))
            self._sync_now.set()
            return None

    @staticmethod
    def _statuses(response: ExchangeResponse) -> list[Any] | None:
        data = response.data
        statuses = data.get("statuses") if isinstance(data, dict) else None
        return statuses if isinstance(statuses, list) else None

    def _settle(self, coin: str, side: Side, status: Any) -> None:
        """Применить статус одной заявки из ответа биржи."""
        st = self.coins[coin]
        slot = st.slots[side]
        if slot is None:
            return
        try:
            outcome = parse_placement_status(status)
        except PlacementFormatError:
            self._sync_now.set()
            return
        if outcome.kind is PlacementKind.RESTING:
            slot.oid, slot.pending = outcome.oid, False
        elif outcome.kind is PlacementKind.ERROR:
            # ALO отклоняется, если пересёк бы стакан; перестановка — если заявка уже
            # исполнена/снята. Слот пуст, сверка уточнит позицию.
            logfire.info("mm: {coin} {side}: {e}", coin=coin, side=side.value, e=outcome.error)
            st.slots[side] = None
            self._sync_now.set()
        else:  # исполнена сразу (для ALO невозможно) или принята без oid
            self._sync_now.set()

    async def _place(self, coin: str, quotes: list[tuple[Side, Quote]]) -> None:
        st = self.coins[coin]
        requests = []
        for side, quote in quotes:
            cloid = actions.new_cloid()
            st.slots[side] = Slot(cloid, quote.price, quote.size, since=self.mono())
            st.last_action[side] = self.mono()
            requests.append(self._request(coin, side, quote, cloid))
        try:
            response = await self._submit(actions.order_action(requests), units=len(requests))
        except (ExchangeNotSentError, HyperliquidError) as exc:
            logfire.warn("mm: заявки {coin} не отправлены: {e}", coin=coin, e=str(exc))
            for side, _ in quotes:
                st.slots[side] = None
            return
        if response is None:
            return
        statuses = self._statuses(response) if response.ok else None
        if not response.ok or statuses is None or len(statuses) != len(quotes):
            logfire.warn("mm: заявки {coin}: {r}", coin=coin, r=str(response.raw)[:300])
            if not response.ok:
                for side, _ in quotes:
                    st.slots[side] = None
            self._sync_now.set()
            return
        for (side, _), status in zip(quotes, statuses, strict=True):
            self._settle(coin, side, status)

    async def _modify(self, coin: str, quotes: list[tuple[Side, Quote]]) -> None:
        st = self.coins[coin]
        quotes = [(side, q) for side, q in quotes if st.slots[side] is not None]
        if not quotes:
            return
        modifies: list[tuple[int | str, actions.OrderRequest]] = []
        for side, quote in quotes:
            slot = st.slots[side]
            if slot is None:  # для mypy: отфильтровано выше
                continue
            target: int | str = slot.oid if slot.oid is not None else slot.cloid
            cloid = actions.new_cloid()
            modifies.append((target, self._request(coin, side, quote, cloid)))
            st.slots[side] = Slot(cloid, quote.price, quote.size, since=self.mono())
            st.last_action[side] = self.mono()
        try:
            response = await self._submit(
                actions.batch_modify_action(modifies), units=len(modifies)
            )
        except (ExchangeNotSentError, HyperliquidError) as exc:
            logfire.warn("mm: перестановка {coin} не отправлена: {e}", coin=coin, e=str(exc))
            self._sync_now.set()  # старые заявки остались на бирже — сверка вернёт их в слоты
            return
        if response is None:
            return
        statuses = self._statuses(response) if response.ok else None
        if statuses is None or len(statuses) != len(quotes):
            if not response.ok:
                logfire.warn("mm: перестановка {coin}: {e}", coin=coin, e=response.error)
            self._sync_now.set()
            return
        for (side, _), status in zip(quotes, statuses, strict=True):
            self._settle(coin, side, status)

    async def _cancel(self, coin: str, sides: list[Side]) -> None:
        st = self.coins[coin]
        by_oid = [(st.rules.asset_id, s.oid) for side in sides if (s := st.slots[side]) and s.oid]
        by_cloid = [
            (st.rules.asset_id, s.cloid)
            for side in sides
            if (s := st.slots[side]) and s.oid is None
        ]
        for side in sides:
            if (slot := st.slots[side]) is not None:
                slot.pending, slot.since = True, self.mono()
            st.last_action[side] = self.mono()
        try:
            if by_oid:
                await self._submit(actions.cancel_many_action(by_oid), units=len(by_oid))
            if by_cloid:
                await self._submit(
                    actions.cancel_by_cloid_many_action(by_cloid), units=len(by_cloid)
                )
        except (ExchangeNotSentError, HyperliquidError) as exc:
            logfire.warn("mm: отмена {coin} не отправлена: {e}", coin=coin, e=str(exc))
            for side in sides:
                if (slot := st.slots[side]) is not None:
                    slot.pending = False  # заявка осталась — попробуем снова
            return
        # Отменена или уже исполнена/снята — в любом случае на бирже её больше нет;
        # если исход был неизвестен, сверка вернёт заявку в слот и отмена повторится.
        for side in sides:
            st.slots[side] = None
        self._sync_now.set()

    async def cancel_all(self) -> None:
        async with self._act:
            for coin, st in list(self.coins.items()):
                sides = [s for s in Side if st.slots[s] is not None]
                if sides:
                    await self._cancel(coin, sides)

    async def flatten_all(self) -> None:
        """Закрыть позиции на всех монетах агрессивной IOC reduce-only заявкой."""
        async with self._act:
            for coin in list(self.coins):
                await self._flatten(coin)
        self._sync_now.set()

    async def _flatten(self, coin: str) -> None:
        """IOC reduce-only на весь остаток по цене хуже лучшей на 1% (под self._act)."""
        st = self.coins[coin]
        if st.position == 0 or st.book is None or not st.book.bids or not st.book.asks:
            return
        buy = st.position < 0
        ref = st.book.asks[0][0] if buy else st.book.bids[0][0]
        price = st.rules.round_price(
            ref * (1 + FLATTEN_SLIPPAGE) if buy else ref * (1 - FLATTEN_SLIPPAGE),
            Rounding.DOWN if buy else Rounding.UP,
        )
        request = actions.OrderRequest(
            asset=st.rules.asset_id,
            is_buy=buy,
            price=price,
            size=abs(st.position),
            reduce_only=True,
            order_type=actions.LimitOrderType(actions.Tif.IOC),
            cloid=actions.new_cloid(),
        )
        try:
            response = await self._submit(actions.order_action([request]), units=1)
        except (ExchangeNotSentError, HyperliquidError) as exc:
            logfire.error("mm: закрыть {coin} не удалось: {e}", coin=coin, e=str(exc))
            return
        logfire.warn(
            "mm: закрытие {coin} {size}: {r}",
            coin=coin,
            size=str(st.position),
            r=str(response.raw if response else "исход неизвестен")[:300],
        )
        self._sync_now.set()

    async def halt(self, reason: str, *, flatten: bool) -> None:
        """Стоп до перезапуска и решения оператора: снять заявки, закрыть, запретить входы."""
        self.halt_reason = reason
        logfire.error("mm: стоп — {reason}", reason=reason)
        await self.cancel_all()
        if flatten:
            await self.flatten_all()
        async with self.sm.begin() as db:
            await control.block_entries(db, self.network, actor=MM_ACTOR, reason=reason[:500])
        await self.notifier.send(f"EraTrade: {reason}. Заявки сняты, позиции закрыты.")


def _slot_json(slot: Slot | None) -> dict[str, Any] | None:
    if slot is None:
        return None
    return {"price": str(slot.price), "size": str(slot.size), "pending": slot.pending}
