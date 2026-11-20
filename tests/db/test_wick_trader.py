"""Бот «ловля проколов»: вход ниже цены, продажа на +цель, докупки, стоп, пауза, лимит."""

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import WickConfig
from app.db.repositories import control
from app.domain.account import OpenOrder, Side
from app.wick.trader import WickTrader
from tests.db.test_mm_maker import COIN, Clock, FakeExchange, set_mode
from tests.factories import T0

D = Decimal
USER = "0x" + "bb" * 20


class WickExchange(FakeExchange):
    """Фейковая биржа с trigger-ордерами и ценами всех монет."""

    def __init__(self) -> None:
        super().__init__()
        self.mid = D("0.10000")
        self.entry: D | None = None

    @staticmethod
    def _order(oid: int, wire: dict[str, Any]) -> OpenOrder:
        base = FakeExchange._order(oid, wire)
        trig = wire["t"].get("trigger")
        if not trig:
            return base
        return OpenOrder(
            **{
                **{f: getattr(base, f) for f in base.__dataclass_fields__},
                "is_trigger": True,
                "trigger_price": D(trig["triggerPx"]),
                "order_type": "Stop Market",
            }
        )

    async def get_all_mids(self) -> dict[str, D]:
        return {COIN: self.mid}

    async def get_account_state(self, user: str) -> Any:
        state = await super().get_account_state(user)
        if self.position and self.entry is not None:
            pos = state.positions[0]
            fields = {f: getattr(pos, f) for f in pos.__dataclass_fields__}
            fields["entry_price"] = self.entry
            state = type(state)(
                **{
                    **{f: getattr(state, f) for f in state.__dataclass_fields__},
                    "positions": (type(pos)(**fields),),
                }
            )
        return state

    def fill_buy(self) -> D:
        """Исполнить стоящую покупку: позиция и средняя цена (взвешенная) как на бирже."""
        buy = next(o for o in self.open.values() if o.side is Side.BUY and not o.reduce_only)
        del self.open[buy.oid]
        old_usd = self.position * (self.entry or D(0))
        self.position += buy.size
        self.entry = (old_usd + buy.size * buy.limit_price) / self.position
        return buy.limit_price

    def orders(self) -> dict[str, OpenOrder]:
        out = {}
        for o in self.open.values():
            kind = "stop" if o.is_trigger else ("tp" if o.reduce_only else "buy")
            out[kind] = o
        return out


@pytest.fixture
def scan(tmp_path: Path) -> Path:
    path = tmp_path / "scan.json"
    path.write_text(
        json.dumps(
            {
                "at": "2026-11-19T00:00:00+00:00",
                "coins": [
                    {"coin": COIN, "passed": True, "params": {"step": 0.012, "tp": 0.01}},
                    {"coin": "NEW", "passed": False, "params": {"step": 0.02, "tp": 0.01}},
                ],
            }
        )
    )
    return path


async def make(
    sm: async_sessionmaker[AsyncSession], scan: Path, **cfg: Any
) -> tuple[WickTrader, WickExchange, Clock]:
    ex, clock = WickExchange(), Clock()
    await set_mode(sm, TradingMode.LIVE_ENABLED)
    trader = WickTrader(
        ex,  # type: ignore[arg-type]
        ex,  # type: ignore[arg-type]
        sm,
        WickConfig(**cfg),
        network=Network.MAINNET,
        user=USER,
        ws_url="ws://unused",
        scan_path=scan,
        now=lambda: T0 + timedelta(hours=12),
        mono=clock,
    )
    await trader.refresh_plans()
    return trader, ex, clock


async def test_entry_tp_averaging_and_stop(
    committing_sessionmaker: async_sessionmaker[AsyncSession], scan: Path
) -> None:
    trader, ex, _ = await make(committing_sessionmaker, scan)
    assert list(trader.coins) == [COIN]  # только монета, прошедшая отбор
    await trader.sync()
    buy = ex.orders()["buy"]
    assert buy.limit_price == D("0.09880")  # 0.1 × (1 − 1.2%)
    assert buy.limit_price * buy.size >= 12 - D("0.01")

    first = ex.fill_buy()
    await trader.sync()
    o = ex.orders()
    assert o["tp"].reduce_only and o["tp"].size == ex.position
    assert o["tp"].limit_price == D("0.09979")  # средняя × 1.01, вверх по шагу цены
    assert o["buy"].limit_price == D("0.09761")  # 0.0988 × (1 − 1.2%), вниз по шагу цены
    assert o["buy"].limit_price * o["buy"].size >= D("14.3")  # вторая покупка в 1.2 раза больше
    assert "stop" not in o
    assert first == D("0.09880")

    ex.fill_buy()
    trader.coins[COIN].last_fill = D("0.09761")
    await trader.sync()
    ex.fill_buy()
    trader.coins[COIN].last_fill = D("0.09643")  # 0.09761 × 0.988
    await trader.sync()
    o = ex.orders()
    assert trader.coins[COIN].level == 3
    assert "buy" not in o and o["stop"].is_trigger and o["stop"].reduce_only
    assert o["stop"].trigger_price == D("0.09528")  # ещё шаг ниже последней покупки
    assert o["tp"].size == ex.position


async def test_pause_cancels_buys_but_keeps_tp(
    committing_sessionmaker: async_sessionmaker[AsyncSession], scan: Path
) -> None:
    trader, ex, _ = await make(committing_sessionmaker, scan)
    await trader.sync()
    ex.fill_buy()
    await trader.sync()
    assert set(ex.orders()) == {"tp", "buy"}
    await set_mode(committing_sessionmaker, TradingMode.LIVE_PAUSED)
    await trader.sync()
    assert set(ex.orders()) == {"tp"}  # продажа защищает позицию и на паузе


async def test_entry_follows_price_with_tolerance(
    committing_sessionmaker: async_sessionmaker[AsyncSession], scan: Path
) -> None:
    trader, ex, _ = await make(committing_sessionmaker, scan)
    await trader.sync()
    n = len(ex.actions)
    trader.coins[COIN].mid = D("0.10020")  # +0.2% < четверти шага (0.3%) — не переставляем
    await trader.sync()
    assert len(ex.actions) == n
    trader.coins[COIN].mid = D("0.10100")  # +1% — переставляем
    await trader.sync()
    assert ex.of_type("batchModify") and ex.orders()["buy"].limit_price == D("0.09978")


async def test_daily_loss_halts_flattens_and_blocks(
    committing_sessionmaker: async_sessionmaker[AsyncSession], scan: Path
) -> None:
    trader, ex, _ = await make(committing_sessionmaker, scan)
    await trader.sync()
    ex.fill_buy()
    ex.unrealized = D("-5.5")
    await trader.sync()
    assert trader.halt_reason and "убыток" in trader.halt_reason
    ioc = [
        o
        for a in ex.of_type("order")
        for o in a["orders"]
        if o["t"].get("limit", {}).get("tif") == "Ioc"
    ]
    assert len(ioc) == 1 and ioc[0]["r"] is True and ioc[0]["b"] is False
    async with committing_sessionmaker() as db:
        state = await control.get_bot_state(db)
    assert state is not None and state.entries_blocked


async def test_zero_daily_loss_never_halts(
    committing_sessionmaker: async_sessionmaker[AsyncSession], scan: Path
) -> None:
    trader, ex, _ = await make(committing_sessionmaker, scan, daily_loss_usd=0)
    await trader.sync()
    ex.fill_buy()
    ex.unrealized = D("-1000")
    await trader.sync()
    assert trader.halt_reason is None
    async with committing_sessionmaker() as db:
        state = await control.get_bot_state(db)
    assert state is not None and not state.entries_blocked


async def test_foreign_orders_untouched_and_oversized_position_stops_buying(
    committing_sessionmaker: async_sessionmaker[AsyncSession], scan: Path
) -> None:
    trader, ex, _ = await make(committing_sessionmaker, scan)
    # ручная заявка из веб-интерфейса (без cloid бота)
    wire = {
        "a": 5,
        "b": True,
        "p": "0.09000",
        "s": "500",
        "r": False,
        "t": {"limit": {"tif": "Gtc"}},
    }
    manual = WickExchange._order(77, wire)
    ex.open[77] = OpenOrder(
        **{**{f: getattr(manual, f) for f in manual.__dataclass_fields__}, "cloid": None}
    )
    # позиция втрое больше, чем бот набирает сам (≈ 44 USDC на все уровни)
    ex.position, ex.entry = D(1500), D("0.1")
    await trader.sync()
    st = trader.coins[COIN]
    assert 77 in ex.open and st.foreign == 1  # чужую заявку не тронули
    assert st.oversized and "ручная торговля" in st.note
    o = {k: v for k, v in ex.orders().items() if v.oid != 77}
    assert "buy" not in o and o["tp"].size == D(1500)  # продажа защищает, докупок нет
