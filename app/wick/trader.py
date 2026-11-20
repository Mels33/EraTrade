"""Живой бот «ловля проколов с усреднением» на монетах из отбора (eratrade wick-scan).

Источник истины — биржа: каждые sync_seconds бот читает позицию и открытые заявки по каждой
монете и приводит их к нужному набору:
- нет позиции — лимит на покупку на шаг ниже цены (переставляется, когда цена уходит на
  requote_frac шага);
- позиция — продажа всей позиции лимитом на +цель от средней (reduce-only), следующая докупка
  на шаг ниже последней покупки; после последнего уровня — стоп на бирже (trigger, reduce-only):
  сработает, даже если бот упал.

Режим: покупки только в LIVE_ENABLED без запрета входов; пауза/запрет снимают покупки, но не
продажу и стоп; DISABLED — снять всё и закрыть позиции. Дневной убыток → снять покупки,
закрыть позиции, запретить входы. Повторной отправки нет: исход неизвестен — разберёт сверка.
"""

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import WickConfig
from app.db.repositories import control
from app.db.repositories import mm as fills_repo
from app.domain.account import OpenOrder, Side
from app.domain.control import Actor, ActorKind
from app.domain.instruments import InstrumentRules, Rounding
from app.execution.session import TradingSession
from app.integrations.hyperliquid import actions, parsing
from app.integrations.hyperliquid.errors import ExchangeOutcomeUnknownError, HyperliquidError
from app.integrations.hyperliquid.exchange_client import ExchangeResponse
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.ws import (
    Connector,
    default_connector,
    iter_messages,
    subscribe_message,
)
from app.mm.maker import ActionBudget
from app.observability.notify import LogNotifier, Notifier
from app.wick.strategy import (
    CoinPlan,
    entry_price,
    level_size,
    levels_filled,
    next_level_price,
    stop_price,
    target_price,
)

COMPONENT = "wick"
WICK_ACTOR = Actor(ActorKind.SYSTEM, "wick")
FLATTEN_SLIPPAGE = Decimal("0.01")
OVERSIZE_FACTOR = Decimal("1.15")  # запас над суммой всех уровней (округления размеров)
PLANS_RELOAD_SECONDS = 3600
BACKFILL_SECONDS = 60


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class CoinState:
    plan: CoinPlan
    rules: InstrumentRules
    mid: Decimal | None = None
    position: Decimal = Decimal(0)
    entry_px: Decimal | None = None
    unrealized: Decimal = Decimal(0)
    level: int = 0
    last_fill: Decimal | None = None
    buy: OpenOrder | None = None
    tp: OpenOrder | None = None
    stop: OpenOrder | None = None
    retiring: bool = False
    note: str = ""
    extra: list[OpenOrder] = field(default_factory=list)
    foreign: int = 0  # заявки не бота (без его cloid): не трогаем
    oversized: bool = False  # позиция больше, чем бот набирает сам (ручная торговля?)


def load_plans(cfg: WickConfig, path: Path) -> list[CoinPlan]:
    """Монеты и их шаг/цель из отбора: прошедшие проверку, лучшие первыми."""
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    rows = [c for c in data.get("coins", []) if c.get("passed") and c.get("params")]
    if cfg.coins:
        by_coin = {c["coin"]: c for c in data.get("coins", []) if c.get("params")}
        rows = [by_coin[c] for c in cfg.coins if c in by_coin]
    return [
        CoinPlan(
            coin=c["coin"],
            step=Decimal(str(c["params"]["step"])),
            tp=Decimal(str(c["params"]["tp"])),
            levels=cfg.levels,
            base_usd=cfg.base_usd,
            size_mult=cfg.size_mult,
        )
        for c in rows[: cfg.max_coins]
    ]


class WickTrader:
    def __init__(
        self,
        session: TradingSession,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        cfg: WickConfig,
        *,
        network: Network,
        user: str,
        ws_url: str,
        scan_path: Path,
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
        self.scan_path = scan_path
        self.notifier = notifier or LogNotifier()
        self.connector = connector
        self.now = now
        self.mono = mono
        self.coins: dict[str, CoinState] = {}
        self.known: set[str] = set()
        self.budget = ActionBudget(cfg.max_actions_per_minute, mono)
        self.instance_id = uuid.uuid4().hex[:12]
        self.started_at = now()
        self.mode: str | None = None
        self.allowed = False
        self.halt_reason: str | None = None
        self.realized_today = Decimal(0)
        self.spot_usdc: Decimal | None = None
        self.scan_at: str | None = None
        self._sync_now = asyncio.Event()
        self._last_plans = -1e9
        self._scan_mtime: float | None = None
        self._last_backfill = 0.0
        self._flattened_for_disabled = False
        self._ws_connected = False
        self._markets: dict[str, Any] = {}

    # --- монеты ---

    async def refresh_plans(self) -> None:
        plans = load_plans(self.cfg, self.scan_path)
        if self.scan_path.exists():
            self.scan_at = json.loads(self.scan_path.read_text()).get("at")
        wanted = {p.coin: p for p in plans}
        if not self._markets:
            self._markets = {m.coin: m for m in await self.gateway.get_markets()}
        for coin, plan in wanted.items():
            if coin not in self._markets:
                continue
            st = self.coins.get(coin)
            if st is None:
                self.coins[coin] = CoinState(plan, InstrumentRules.for_perp(self._markets[coin]))
                self.known.add(coin)
                await self._set_leverage(coin)
            else:
                st.plan, st.retiring = plan, False
        for coin, st in self.coins.items():
            if coin not in wanted:
                st.retiring = True

    async def adopt_previous(self) -> None:
        """Монеты прошлого запуска с позицией — под контроль (по heartbeat в БД)."""
        async with self.sm() as db:
            beat = next(
                (h for h in await control.get_heartbeats(db) if h.component == COMPONENT), None
            )
        previous = beat.details.get("coins") or {} if beat else {}
        if not previous:
            return
        if not self._markets:
            self._markets = {m.coin: m for m in await self.gateway.get_markets()}
        account = await self.gateway.get_account_state(self.user)
        for coin, info in previous.items():
            pos = account.position(coin)
            if coin in self.coins or pos is None or pos.size == 0 or coin not in self._markets:
                continue
            plan = CoinPlan(
                coin=coin,
                step=Decimal(str(info.get("step", "0.012"))),
                tp=Decimal(str(info.get("tp", "0.01"))),
                levels=self.cfg.levels,
                base_usd=self.cfg.base_usd,
                size_mult=self.cfg.size_mult,
            )
            logfire.warn(
                "wick: подхватываю {coin} с позицией {size}", coin=coin, size=str(pos.size)
            )
            self.coins[coin] = CoinState(
                plan, InstrumentRules.for_perp(self._markets[coin]), retiring=True
            )
            self.known.add(coin)

    async def _set_leverage(self, coin: str) -> None:
        st = self.coins[coin]
        try:
            response = await self.session.submit(
                actions.update_leverage_action(
                    st.rules.asset_id, is_cross=False, leverage=self.cfg.leverage
                )
            )
            if not response.ok:
                logfire.warn("wick: плечо {coin}: {e}", coin=coin, e=response.error)
        except HyperliquidError as exc:
            logfire.warn("wick: плечо {coin}: {e}", coin=coin, e=str(exc))

    # --- запуск ---

    async def run(self) -> None:
        await self.adopt_previous()
        await self.refresh_plans()
        logfire.info("wick: запущен, монеты {coins}", coins=list(self.coins))
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._ws_forever())
                tg.create_task(self._sync_forever())
        finally:
            await asyncio.shield(self._shutdown())

    async def _shutdown(self) -> None:
        """Снять только покупки: продажа и стоп защищают позицию и без бота."""
        try:
            for coin, st in list(self.coins.items()):
                if st.buy is not None:
                    await self._cancel(coin, [st.buy])
        except Exception as exc:
            logfire.error("wick: при остановке покупки не сняты: {e}", e=repr(exc))

    async def _ws_forever(self) -> None:
        delay = 1.0
        while True:
            try:
                conn = await self.connector(self.ws_url)
                try:
                    await conn.send(subscribe_message({"type": "allMids"}))
                    await conn.send(subscribe_message({"type": "userFills", "user": self.user}))
                    self._ws_connected = True
                    delay = 1.0
                    async for channel, data in iter_messages(
                        conn, silence_timeout=30, ping_interval=20
                    ):
                        await self.handle(channel, data)
                finally:
                    self._ws_connected = False
                    await conn.close()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logfire.warn(
                    "wick: WebSocket: {e}, переподключение через {d} с", e=repr(exc), d=delay
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)

    async def handle(self, channel: str, data: Any) -> None:
        if channel == "allMids" and isinstance(data, dict):
            mids = parsing.parse_all_mids(data.get("mids") or {})
            for coin, st in self.coins.items():
                if coin in mids:
                    st.mid = mids[coin]
        elif channel == "userFills" and isinstance(data, dict):
            fills = [parsing.parse_fill(f) for f in data.get("fills") or []]
            ours = [f for f in fills if f.coin in self.known]
            if not ours:
                return
            async with self.sm.begin() as db:
                added = await fills_repo.add_fills(db, ours)
            if data.get("isSnapshot"):
                return
            for f in ours:
                if f.tid in added and f.side is Side.BUY and f.coin in self.coins:
                    self.coins[f.coin].last_fill = f.price
            if added:
                logfire.info("wick: исполнений {n}", n=len(added))
                self._sync_now.set()

    # --- сверка и заявки ---

    async def _sync_forever(self) -> None:
        while True:
            try:
                await self.sync()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logfire.error("wick: сверка не удалась: {e}", e=repr(exc))
            self._sync_now.clear()
            try:
                await asyncio.wait_for(self._sync_now.wait(), timeout=self.cfg.sync_seconds)
            except TimeoutError:
                pass

    async def sync(self) -> None:
        mono = self.mono()
        mtime = self.scan_path.stat().st_mtime if self.scan_path.exists() else None
        if mtime != self._scan_mtime or mono - self._last_plans >= PLANS_RELOAD_SECONDS:
            if mtime != self._scan_mtime and self._scan_mtime is not None:
                logfire.info("wick: новый отбор монет — перечитываю")
            self._last_plans, self._scan_mtime = mono, mtime
            await self.refresh_plans()
        async with self.sm() as db:
            state = await control.get_bot_state(db)
        self.mode = state.trading_mode if state else None
        self.allowed = (
            self.mode == TradingMode.LIVE_ENABLED.value
            and not (state and state.entries_blocked)
            and self.halt_reason is None
        )
        account = await self.gateway.get_account_state(self.user)
        orders = await self.gateway.get_open_orders(self.user)
        if self.coins and any(st.mid is None for st in self.coins.values()):
            mids = await self.gateway.get_all_mids()
            for coin, st in self.coins.items():
                st.mid = st.mid or mids.get(coin)
        for coin, st in self.coins.items():
            pos = account.position(coin)
            st.position = pos.size if pos else Decimal(0)
            st.entry_px = pos.entry_price if pos else None
            st.unrealized = pos.unrealized_pnl if pos else Decimal(0)
            self._classify(st, [o for o in orders if o.coin == coin])
            if st.position > 0 and st.entry_px:
                st.level = levels_filled(st.plan, st.position * st.entry_px)
                if st.last_fill is None:
                    async with self.sm() as db:
                        st.last_fill = await fills_repo.last_buy_price(db, coin) or st.entry_px
            else:
                st.level, st.last_fill = 0, None

        if mono - self._last_backfill >= BACKFILL_SECONDS:
            self._last_backfill = mono
            await self._backfill()
            self.spot_usdc = await self.gateway.get_spot_usdc(self.user)

        day_start = self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.sm() as db:
            self.realized_today = await fills_repo.realized_since(db, day_start)
        pnl = self.realized_today + sum((st.unrealized for st in self.coins.values()), Decimal(0))
        if (
            self.cfg.daily_loss_usd > 0
            and self.halt_reason is None
            and pnl <= -self.cfg.daily_loss_usd
        ):
            await self.halt(f"ловля проколов: дневной убыток {pnl:.2f} USDC")

        if self.mode == TradingMode.DISABLED.value:
            if not self._flattened_for_disabled:
                self._flattened_for_disabled = True
                for coin, st in list(self.coins.items()):
                    await self._cancel(coin, [o for o in (st.buy, st.tp, st.stop, *st.extra) if o])
                    await self._flatten(coin)
        else:
            self._flattened_for_disabled = False
            for coin in list(self.coins):
                await self.reconcile(coin)
        # монету, с которой ушли, убираем, когда позиция закрыта и заявок нет
        for coin, st in list(self.coins.items()):
            if st.retiring and st.position == 0 and not (st.buy or st.tp or st.stop):
                del self.coins[coin]
        await self._beat(pnl)

    @staticmethod
    def _classify(st: CoinState, orders: list[OpenOrder]) -> None:
        st.buy = st.tp = st.stop = None
        st.extra = []
        st.foreign = 0
        for o in sorted(orders, key=lambda o: o.created_at, reverse=True):
            if not o.cloid:
                # заявка не бота (веб-интерфейс, другая программа) — не переставляем и не снимаем
                st.foreign += 1
                continue
            if o.is_trigger:
                slot = "stop"
            elif o.side is Side.SELL and o.reduce_only:
                slot = "tp"
            elif o.side is Side.BUY and not o.reduce_only:
                slot = "buy"
            else:
                st.extra.append(o)
                continue
            if getattr(st, slot) is None:
                setattr(st, slot, o)
            else:
                st.extra.append(o)  # дубль — снимем

    async def reconcile(self, coin: str) -> None:
        st = self.coins[coin]
        if st.extra:
            await self._cancel(coin, st.extra)
        if st.mid is None:
            return
        if st.position < 0:  # стратегия только long — непредвиденный short закрываем
            logfire.warn("wick: {coin} short {size} — закрываю", coin=coin, size=str(st.position))
            await self._flatten(coin)
            return
        rules, plan = st.rules, st.plan
        # продажа на +цель от средней и стоп после последнего уровня — всегда при позиции
        if st.position > 0 and st.entry_px:
            tp_px = target_price(rules, plan, st.entry_px)
            await self._ensure_limit(coin, "tp", Side.SELL, tp_px, st.position, reduce_only=True)
            if st.level >= plan.levels and st.last_fill:
                trig = stop_price(rules, plan, st.last_fill)
                await self._ensure_stop(coin, trig, st.position)
            elif st.stop is not None:
                await self._cancel(coin, [st.stop])
        else:
            for o in (st.tp, st.stop):
                if o is not None:
                    await self._cancel(coin, [o])
        # покупка: вход или следующая докупка; позиция больше, чем бот набирает сам, —
        # скорее всего ручная торговля на этой монете: докупки выключаем
        limit_usd = plan.cum_usd(plan.levels) * OVERSIZE_FACTOR
        st.oversized = st.position * st.mid > limit_usd
        want: tuple[Decimal, Decimal] | None = None
        if self.allowed and not st.oversized:
            if st.position == 0 and not st.retiring:
                px = entry_price(rules, plan, st.mid)
                want = (px, level_size(rules, plan, 0, px))
            elif st.position > 0 and st.level < plan.levels and st.last_fill:
                px = next_level_price(rules, plan, st.last_fill)
                want = (px, level_size(rules, plan, st.level, px))
        if want is None:
            if st.buy is not None:
                await self._cancel(coin, [st.buy])
            if st.oversized:
                st.note = (
                    f"позиция ${st.position * st.mid:.0f} больше, чем набирает бот "
                    f"(${limit_usd:.0f}) — ручная торговля? Докупки выключены"
                )
            else:
                st.note = "" if self.allowed else "покупки выключены"
            return
        px, size = want
        if st.position == 0 and st.buy is not None:
            # вход следует за ценой с допуском: перестановка, когда цена ушла на долю шага
            tolerance = st.mid * plan.step * self.cfg.requote_frac
            if abs(st.buy.limit_price - px) <= tolerance:
                return  # размер (номинал / цена) в пределах допуска не пересчитываем
        await self._ensure_limit(coin, "buy", Side.BUY, px, size, reduce_only=False)

    async def _ensure_limit(
        self, coin: str, slot: str, side: Side, price: Decimal, size: Decimal, *, reduce_only: bool
    ) -> None:
        st = self.coins[coin]
        current: OpenOrder | None = getattr(st, slot)
        if current is not None and current.limit_price == price and current.size == size:
            return
        request = actions.OrderRequest(
            asset=st.rules.asset_id,
            is_buy=side is Side.BUY,
            price=price,
            size=size,
            reduce_only=reduce_only,
            order_type=actions.LimitOrderType(actions.Tif.GTC),
            cloid=actions.new_cloid(),
        )
        if current is None:
            await self._submit(coin, actions.order_action([request]))
        else:
            await self._submit(coin, actions.batch_modify_action([(current.oid, request)]))

    async def _ensure_stop(self, coin: str, trigger: Decimal, size: Decimal) -> None:
        st = self.coins[coin]
        if st.stop is not None and st.stop.trigger_price == trigger and st.stop.size == size:
            return
        if st.stop is not None:
            await self._cancel(coin, [st.stop])
        limit = st.rules.round_price(trigger * (1 - self.cfg.stop_slippage), Rounding.UP)
        request = actions.OrderRequest(
            asset=st.rules.asset_id,
            is_buy=False,
            price=limit,
            size=size,
            reduce_only=True,
            order_type=actions.TriggerOrderType(trigger_price=trigger, is_market=True, tpsl="sl"),
            cloid=actions.new_cloid(),
        )
        await self._submit(coin, actions.order_action([request]))

    async def _cancel(self, coin: str, orders: list[OpenOrder]) -> None:
        if orders:
            asset = self.coins[coin].rules.asset_id
            await self._submit(
                coin, actions.cancel_many_action([(asset, o.oid) for o in orders]), force=True
            )

    async def _flatten(self, coin: str) -> None:
        st = self.coins[coin]
        if st.position == 0 or st.mid is None:
            return
        buy = st.position < 0
        price = st.rules.round_price(
            st.mid * (1 + FLATTEN_SLIPPAGE) if buy else st.mid * (1 - FLATTEN_SLIPPAGE),
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
        await self._submit(coin, actions.order_action([request]), force=True)

    async def _submit(
        self, coin: str, action: dict[str, Any], *, force: bool = False
    ) -> ExchangeResponse | None:
        units = len(action.get("orders") or action.get("cancels") or action.get("modifies") or [1])
        if not force and not self.budget.allows(units):
            self.coins[coin].note = "ждёт бюджета действий"
            return None
        self.budget.spend(units)
        try:
            response = await self.session.submit(action)
        except ExchangeOutcomeUnknownError as exc:
            logfire.warn(
                "wick: {coin} исход {t} неизвестен: {e}", coin=coin, t=action["type"], e=str(exc)
            )
            self._sync_now.set()
            return None
        except HyperliquidError as exc:
            logfire.warn(
                "wick: {coin} {t} не отправлено: {e}", coin=coin, t=action["type"], e=str(exc)
            )
            return None
        if not response.ok:
            logfire.warn("wick: {coin} {t}: {e}", coin=coin, t=action["type"], e=response.error)
        else:
            statuses = (
                (response.data or {}).get("statuses") if isinstance(response.data, dict) else None
            )
            errors = [s for s in statuses or [] if isinstance(s, dict) and "error" in s]
            if errors:
                logfire.info(
                    "wick: {coin} {t}: {e}", coin=coin, t=action["type"], e=str(errors)[:200]
                )
        return response

    async def _backfill(self) -> None:
        async with self.sm() as db:
            last = await fills_repo.last_fill_time(db)
        since = last or self.now().replace(hour=0, minute=0, second=0, microsecond=0)
        fills = [f for f in await self.gateway.get_fills(self.user, since) if f.coin in self.known]
        async with self.sm.begin() as db:
            await fills_repo.add_fills(db, fills)

    async def halt(self, reason: str) -> None:
        self.halt_reason = reason
        self.allowed = False
        logfire.error("wick: стоп — {reason}", reason=reason)
        for coin, st in list(self.coins.items()):
            await self._cancel(coin, [o for o in (st.buy, st.tp, st.stop) if o])
            await self._flatten(coin)
        async with self.sm.begin() as db:
            await control.block_entries(db, self.network, actor=WICK_ACTOR, reason=reason[:500])
        await self.notifier.send(f"EraTrade: {reason}. Заявки сняты, позиции закрыты.")

    def status_text(self) -> str:
        if self.halt_reason:
            return f"остановлен: {self.halt_reason}"
        if self.mode != TradingMode.LIVE_ENABLED.value:
            return f"покупки выключены: режим {self.mode}"
        if not self.allowed:
            return "покупки выключены: запрет входов"
        return "ловит проколы"

    async def _beat(self, pnl: Decimal) -> None:
        coins = {}
        for coin, st in self.coins.items():
            rules, plan = st.rules, st.plan
            coins[coin] = {
                "mid": str(st.mid) if st.mid else None,
                "step": str(plan.step),
                "tp": str(plan.tp),
                "levels": plan.levels,
                "level": st.level,
                "position": str(st.position),
                "position_usd": str((st.position * st.mid).quantize(Decimal("0.01")))
                if st.mid
                else None,
                "entry_px": str(st.entry_px) if st.entry_px else None,
                "unrealized": str(st.unrealized),
                "buy": {"price": str(st.buy.limit_price), "size": str(st.buy.size)}
                if st.buy
                else None,
                "tp_order": str(st.tp.limit_price) if st.tp else None,
                "stop_order": str(st.stop.trigger_price)
                if st.stop and st.stop.trigger_price
                else None,
                "next_stop": str(stop_price(rules, plan, st.last_fill)) if st.last_fill else None,
                "retiring": st.retiring,
                "note": st.note,
                "foreign_orders": st.foreign,
                "oversized": st.oversized,
            }
        details = {
            "state": self.status_text(),
            "quoting": self.allowed,
            "pnl_today": str(pnl.quantize(Decimal("0.0001"))),
            "realized_today": str(self.realized_today.quantize(Decimal("0.0001"))),
            "daily_loss_limit": str(self.cfg.daily_loss_usd),
            "spot_usdc": str(self.spot_usdc) if self.spot_usdc is not None else None,
            "actions_last_min": self.budget.used(),
            "actions_limit": self.cfg.max_actions_per_minute,
            "ws": self._ws_connected,
            "scan_at": self.scan_at,
            "levels_usd": [
                str(round(self.cfg.base_usd * self.cfg.size_mult**k, 2))
                for k in range(self.cfg.levels)
            ],
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
