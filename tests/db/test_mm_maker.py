"""Маркет-мейкер с фейковой биржей: котировки, перестановки, пауза, исполнения, стоп по убытку."""

import itertools
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import MmConfig
from app.db.models import MmFillRecord
from app.db.repositories import control
from app.domain.account import AccountState, OpenOrder, Position, RateLimitStatus, Side
from app.domain.instruments import InstrumentRules
from app.integrations.hyperliquid.exchange_client import ExchangeResponse
from app.integrations.hyperliquid.parsing import datetime_to_ms
from app.mm.maker import MarketMaker
from app.mm.scanner import MarketScan
from tests.factories import T0, market_meta

D = Decimal
COIN = "FART"
RULES = InstrumentRules(coin=COIN, asset_id=5, sz_decimals=1, max_price_decimals=5)
USER = "0x" + "aa" * 20
ASSETS = {5: COIN, 9: "NEW", 11: "OLD"}


class FakeExchange:
    """Сессия + шлюз: принимает действия, держит открытые заявки и позицию."""

    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []
        self.open: dict[int, OpenOrder] = {}
        self.position = D(0)
        self.unrealized = D(0)
        self.others: dict[str, D] = {}  # позиции на других монетах
        self._oids = itertools.count(1000)

    # --- TradingSession ---
    async def submit(self, action: dict[str, Any]) -> ExchangeResponse:
        self.actions.append(action)
        kind = action["type"]
        statuses: list[Any] = []
        if kind == "order":
            for o in action["orders"]:
                if "Ioc" in str(o["t"]):
                    statuses.append(
                        {"filled": {"oid": next(self._oids), "totalSz": o["s"], "avgPx": o["p"]}}
                    )
                    continue
                oid = next(self._oids)
                self.open[oid] = self._order(oid, o)
                statuses.append({"resting": {"oid": oid}})
        elif kind == "batchModify":
            for m in action["modifies"]:
                old = next((x for x in self.open.values() if m["oid"] in (x.oid, x.cloid)), None)
                if old is None:
                    statuses.append({"error": "Cannot modify canceled or filled order"})
                    continue
                del self.open[old.oid]
                oid = next(self._oids)
                self.open[oid] = self._order(oid, m["order"])
                statuses.append({"resting": {"oid": oid}})
        elif kind == "cancel":
            for c in action["cancels"]:
                statuses.append("success" if self.open.pop(c["o"], None) else {"error": "x"})
        elif kind == "cancelByCloid":
            for c in action["cancels"]:
                hit = next((x for x in self.open.values() if x.cloid == c["cloid"]), None)
                if hit:
                    del self.open[hit.oid]
                statuses.append("success" if hit else {"error": "x"})
        return ExchangeResponse(True, kind, {"statuses": statuses}, None, {"status": "ok"})

    @staticmethod
    def _order(oid: int, wire: dict[str, Any]) -> OpenOrder:
        return OpenOrder(
            coin=ASSETS.get(wire.get("a", 5), COIN),
            side=Side.BUY if wire["b"] else Side.SELL,
            limit_price=D(wire["p"]),
            size=D(wire["s"]),
            original_size=D(wire["s"]),
            oid=oid,
            cloid=wire.get("c"),
            created_at=T0,
            order_type="Limit",
            tif="Alo",
            reduce_only=wire["r"],
            is_trigger=False,
            trigger_price=None,
            trigger_condition="N/A",
            is_position_tpsl=False,
            raw={},
        )

    def of_type(self, kind: str) -> list[dict[str, Any]]:
        return [a for a in self.actions if a["type"] == kind]

    # --- HyperliquidGateway ---
    async def get_markets(self) -> list[Any]:
        return [
            market_meta(coin=c, asset_id=a, sz_decimals=1, max_leverage=10)
            for a, c in ASSETS.items()
        ]

    async def get_account_state(self, user: str) -> AccountState:
        sizes = {COIN: self.position, **self.others}
        positions = tuple(
            Position(
                coin=coin,
                size=size,
                entry_price=D("0.0865"),
                position_value=abs(size) * D("0.0865"),
                unrealized_pnl=self.unrealized if coin == COIN else D(0),
                liquidation_price=None,
                margin_used=D(4),
                leverage_type="isolated",
                leverage=3,
                max_leverage=10,
                funding_since_open=D(0),
                raw={},
            )
            for coin, size in sizes.items()
            if size
        )
        return AccountState(D(98), D(0), D(0), D(0), D(98), positions, T0, raw={})

    async def get_open_orders(self, user: str) -> list[OpenOrder]:
        return list(self.open.values())

    async def get_fills(self, user: str, start: Any, end: Any = None) -> list[Any]:
        return []

    async def get_spot_usdc(self, user: str) -> Decimal:
        return D("98.4")

    async def get_user_rate_limit(self, user: str) -> RateLimitStatus:
        return RateLimitStatus(D(0), 1000, 200_000, 0)


class Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def l2(bid: str, ask: str, coin: str = COIN) -> dict[str, Any]:
    return {
        "coin": coin,
        "time": datetime_to_ms(T0),
        "levels": [
            [
                {"px": bid, "sz": "1000", "n": 3},
                {"px": str(D(bid) - D("0.00001")), "sz": "9000", "n": 5},
            ],
            [
                {"px": ask, "sz": "1000", "n": 3},
                {"px": str(D(ask) + D("0.00001")), "sz": "9000", "n": 5},
            ],
        ],
    }


async def set_mode(
    sm: async_sessionmaker[AsyncSession], mode: TradingMode, blocked: bool = False
) -> None:
    async with sm.begin() as db:
        state = await control.ensure_bot_state(db, Network.MAINNET)
        state.trading_mode = mode.value
        state.entries_blocked = blocked
        state.block_reason = "тест" if blocked else None


@pytest.fixture
async def maker(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> tuple[MarketMaker, FakeExchange, Clock]:
    ex, clock = FakeExchange(), Clock()
    await set_mode(committing_sessionmaker, TradingMode.LIVE_ENABLED)
    mm = MarketMaker(
        ex,  # type: ignore[arg-type]
        ex,  # type: ignore[arg-type]
        committing_sessionmaker,
        MmConfig(coins=(COIN,), requote_min_interval_seconds=3),
        network=Network.MAINNET,
        user=USER,
        ws_url="ws://unused",
        rules={COIN: RULES},
        now=lambda: T0 + timedelta(hours=12),
        mono=clock,
    )
    return mm, ex, clock


async def test_quotes_both_sides_then_pause_cancels(
    maker: tuple[MarketMaker, FakeExchange, Clock],
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    mm, ex, _ = maker
    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()
    [order] = ex.of_type("order")
    tifs = {o["t"]["limit"]["tif"] for o in order["orders"]}
    assert tifs == {"Alo"} and {o["p"] for o in order["orders"]} == {"0.08651", "0.08656"}
    assert len(ex.open) == 2 and all(s and not s.pending for s in mm.coins[COIN].slots.values())

    await mm.quote_once()  # ничего не изменилось — новых действий нет
    assert len(ex.actions) == 1

    await set_mode(committing_sessionmaker, TradingMode.LIVE_PAUSED)
    await mm.sync()
    await mm.quote_once()
    assert ex.of_type("cancel") and ex.open == {}
    assert all(s is None for s in mm.coins[COIN].slots.values())
    async with committing_sessionmaker() as db:
        beat = next(h for h in await control.get_heartbeats(db) if h.component == "mm")
    assert "LIVE_PAUSED" in beat.details["state"]


async def test_retreat_is_immediate_but_chasing_waits_for_interval(
    maker: tuple[MarketMaker, FakeExchange, Clock],
) -> None:
    mm, ex, clock = maker
    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()

    # Рынок пошёл вниз: наш bid 0.08651 выше нового лучшего — отступаем сразу; ask опускать
    # к рынку (догонять) — не раньше интервала.
    clock.t += 0.5
    await mm.handle("l2Book", l2("0.08640", "0.08647"))
    await mm.quote_once()
    [modify] = ex.of_type("batchModify")
    assert [(m["order"]["b"], m["order"]["p"]) for m in modify["modifies"]] == [(True, "0.08641")]

    # Рынок вернулся вверх через 1 с: bid догоняет — ждёт; ask на месте.
    clock.t += 1
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()
    assert len(ex.of_type("batchModify")) == 1
    clock.t += 3
    await mm.quote_once()
    assert len(ex.of_type("batchModify")) == 2
    assert mm.coins[COIN].slots[Side.BUY].price == D("0.08651")  # type: ignore[union-attr]
    assert mm.coins[COIN].slots[Side.SELL].price == D("0.08656")  # type: ignore[union-attr]


async def test_fill_is_recorded_and_moves_position(
    maker: tuple[MarketMaker, FakeExchange, Clock],
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    mm, _, _ = maker
    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()
    bid = mm.coins[COIN].slots[Side.BUY]
    assert bid is not None
    size = bid.size
    fill = {
        "coin": COIN, "side": "B", "px": str(bid.price), "sz": str(bid.size),
        "time": datetime_to_ms(T0 + timedelta(hours=12)), "startPosition": "0",
        "dir": "Open Long", "closedPnl": "0", "fee": "0.0018", "feeToken": "USDC",
        "oid": bid.oid, "tid": 77, "hash": "0x0", "crossed": False, "cloid": bid.cloid,
    }  # fmt: skip
    await mm.handle("userFills", {"isSnapshot": False, "user": USER, "fills": [fill]})
    await mm.handle("userFills", {"isSnapshot": False, "user": USER, "fills": [fill]})  # дубль
    assert mm.coins[COIN].position == size  # повтор с тем же tid не учитывается
    assert mm.coins[COIN].slots[Side.BUY] is None
    async with committing_sessionmaker() as db:
        rows = (await db.execute(select(MmFillRecord))).scalars().all()
    assert [r.tid for r in rows] == [77]


async def test_daily_loss_halts_flattens_and_blocks_entries(
    maker: tuple[MarketMaker, FakeExchange, Clock],
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    mm, ex, _ = maker
    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()
    ex.position, ex.unrealized = D(250), D("-2.1")
    await mm.sync()
    assert mm.halt_reason and "убыток" in mm.halt_reason
    assert ex.open == {}
    ioc = [o for a in ex.of_type("order") for o in a["orders"] if o["t"]["limit"]["tif"] == "Ioc"]
    assert len(ioc) == 1 and ioc[0]["r"] is True and ioc[0]["b"] is False and ioc[0]["s"] == "250"
    async with committing_sessionmaker() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked and "убыток" in (state.block_reason or "")

    n = len(ex.actions)
    await mm.quote_once()
    assert len(ex.actions) == n  # после стопа не котирует


async def test_sync_adopts_and_cancels_extra_orders(
    maker: tuple[MarketMaker, FakeExchange, Clock],
) -> None:
    mm, ex, _ = maker
    for oid, price in ((1, "0.08640"), (2, "0.08645")):
        ex.open[oid] = FakeExchange._order(oid, {"b": True, "p": price, "s": "140", "r": False})
    await mm.startup()
    # обе заявки прошлого запуска сняты, слотов нет
    assert ex.open == {} and mm.coins[COIN].slots[Side.BUY] is None
    assert any(a["type"] == "updateLeverage" for a in ex.actions)


async def test_action_budget_limits_placements(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ex, clock = FakeExchange(), Clock()
    await set_mode(committing_sessionmaker, TradingMode.LIVE_ENABLED)
    mm = MarketMaker(
        ex,  # type: ignore[arg-type]
        ex,  # type: ignore[arg-type]
        committing_sessionmaker,
        MmConfig(coins=(COIN,), max_actions_per_minute=3, requote_min_interval_seconds=0),
        network=Network.MAINNET,
        user=USER,
        ws_url="ws://unused",
        rules={COIN: RULES},
        now=lambda: T0,
        mono=clock,
    )
    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()  # 2 единицы
    clock.t += 1
    await mm.handle("l2Book", l2("0.08640", "0.08647"))
    await mm.quote_once()  # перестановка двух заявок — 2 единицы, бюджет 3 → ждём
    assert ex.of_type("batchModify") == []
    clock.t += 61
    await mm.handle("l2Book", l2("0.08640", "0.08647"))  # иначе стакан устарел бы
    await mm.quote_once()
    assert len(ex.of_type("batchModify")) == 1


def scan_of(coin: str, spread: float, volume_m: float) -> MarketScan:
    return MarketScan(
        coin=coin,
        mid=D("0.0865"),
        spread_bps=D(str(spread)),
        tick_bps=D("1.2"),
        day_volume_usd=D(str(volume_m * 1_000_000)),
        top_depth_usd=D(500),
        max_leverage=10,
        sz_decimals=1,
    )


def auto_maker(
    ex: FakeExchange, clock: Clock, sm: async_sessionmaker[AsyncSession], scans: list[MarketScan]
) -> MarketMaker:
    async def scanner() -> list[MarketScan]:
        return scans

    return MarketMaker(
        ex,  # type: ignore[arg-type]
        ex,  # type: ignore[arg-type]
        sm,
        MmConfig(coins=(COIN,), auto_select=True, max_coins=1, requote_min_interval_seconds=0),
        network=Network.MAINNET,
        user=USER,
        ws_url="ws://unused",
        rules={COIN: RULES},
        scanner=scanner,
        now=lambda: T0 + timedelta(hours=12),
        mono=clock,
    )


async def test_rotation_retires_old_coin_by_closing_and_then_removes_it(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ex, clock = FakeExchange(), Clock()
    ex.position = D(150)  # long ≈ 13 USDC на старой монете
    await set_mode(committing_sessionmaker, TradingMode.LIVE_ENABLED)
    # у старой монеты спред сузился — она больше не кандидат
    mm = auto_maker(
        ex, clock, committing_sessionmaker, [scan_of(COIN, 2, 50), scan_of("NEW", 8, 6)]
    )
    assert await mm.select() == ["NEW"]
    assert mm.coins[COIN].retiring and not mm.coins["NEW"].retiring
    assert mm._resubscribe  # WebSocket переподпишется на новую монету

    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.handle("l2Book", l2("0.08650", "0.08657", coin="NEW"))
    await mm.quote_once()
    old_orders = [o for o in ex.open.values() if o.coin == COIN]
    assert len(old_orders) == 1 and old_orders[0].side is Side.SELL
    assert old_orders[0].reduce_only and old_orders[0].size == D(150)
    assert len([o for o in ex.open.values() if o.coin == "NEW"]) == 2

    # закрывающая заявка исполнилась
    for oid in [o.oid for o in old_orders]:
        del ex.open[oid]
    ex.position = D(0)
    await mm.sync(force_orders=True)
    assert COIN not in mm.coins and "NEW" in mm.coins and COIN in mm.known


async def test_retiring_coin_is_flattened_after_timeout(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ex, clock = FakeExchange(), Clock()
    ex.position = D(150)
    await set_mode(committing_sessionmaker, TradingMode.LIVE_ENABLED)
    mm = auto_maker(
        ex, clock, committing_sessionmaker, [scan_of(COIN, 2, 50), scan_of("NEW", 8, 6)]
    )
    await mm.select()
    await mm.sync()
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()
    clock.t += 61 * 60
    await mm.handle("l2Book", l2("0.08650", "0.08657"))
    await mm.quote_once()
    await mm.sync()
    ioc = [o for a in ex.of_type("order") for o in a["orders"] if o["t"]["limit"]["tif"] == "Ioc"]
    assert len(ioc) == 1 and ioc[0]["a"] == 5 and ioc[0]["r"] is True and ioc[0]["s"] == "150"


async def test_restart_adopts_previous_coin_with_position(
    committing_sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    ex, clock = FakeExchange(), Clock()
    ex.others = {"OLD": D(-140)}
    async with committing_sessionmaker.begin() as db:
        await control.beat(
            db, component="mm", instance_id="prev", started_at=T0, now=T0,
            details={"coins": {"OLD": {}, COIN: {}}},
        )  # fmt: skip
    await set_mode(committing_sessionmaker, TradingMode.LIVE_ENABLED)
    mm = auto_maker(ex, clock, committing_sessionmaker, [scan_of(COIN, 8, 6)])
    await mm.startup()
    assert "OLD" in mm.coins and mm.coins["OLD"].retiring
    assert mm.coins["OLD"].position == D(-140)
    assert COIN in mm.coins and not mm.coins[COIN].retiring
