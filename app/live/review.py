"""Разбор живых сделок (§17.2): совпадают ли размер, стоп, fills, комиссии и результат.

Сделка — эпизод позиции от нуля до нуля по исполнениям (fills). Для каждой:
- вход/выход: средние цены, размер, номинал, время удержания;
- комиссии, funding, реализованный PnL биржи и итог нетто;
- проверка размера: убыток до запланированного стопа не больше риска на сделку,
  номинал не больше лимита;
- проверка стопа: защитный stop-market выставлен на весь размер;
- проскальзывание входа против цены превью (mid) и против close свечи сигнала;
- как закрыта: выход по сигналу, стоп или вручную на бирже.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.strategy import StrategyConfig
from app.db.models import FillRecord, FundingEventRecord, OrderRecord, SignalRecord
from app.domain.orders import OrderPurpose


@dataclass
class RoundTrip:
    side: str  # long | short
    opened_at: datetime
    closed_at: datetime | None
    size: Decimal
    entry_price: Decimal
    exit_price: Decimal | None
    fees: Decimal
    funding: Decimal
    realized_pnl: Decimal
    exit_kind: str | None  # exit | stop | manual
    bot_entry: bool
    checks: list[str] = field(default_factory=list)  # найденные несоответствия
    notes: list[str] = field(default_factory=list)
    entry_slippage_vs_preview_bps: float | None = None
    entry_slippage_vs_signal_bps: float | None = None
    signal_decision_time: datetime | None = None

    @property
    def net(self) -> Decimal:
        return self.realized_pnl - self.fees + self.funding

    @property
    def notional(self) -> Decimal:
        return self.size * self.entry_price

    def as_dict(self) -> dict[str, Any]:
        def s(v: Decimal | None) -> str | None:
            return None if v is None else str(v)

        return {
            "side": self.side,
            "opened_at": self.opened_at.isoformat(),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "size": s(self.size),
            "notional": s(self.notional.quantize(Decimal("0.01"))),
            "entry_price": s(self.entry_price),
            "exit_price": s(self.exit_price),
            "fees": s(self.fees),
            "funding": s(self.funding),
            "realized_pnl": s(self.realized_pnl),
            "net": s(self.net),
            "exit_kind": self.exit_kind,
            "bot_entry": self.bot_entry,
            "checks": self.checks,
            "notes": self.notes,
            "entry_slippage_vs_preview_bps": self.entry_slippage_vs_preview_bps,
            "entry_slippage_vs_signal_bps": self.entry_slippage_vs_signal_bps,
            "signal_decision_time": (
                self.signal_decision_time.isoformat() if self.signal_decision_time else None
            ),
        }


def _vwap(fills: Sequence[FillRecord]) -> Decimal:
    size = sum((f.size for f in fills), Decimal(0))
    return sum((f.price * f.size for f in fills), Decimal(0)) / size


def _bps(price: Decimal, reference: Decimal, *, buy: bool) -> float:
    diff = price - reference if buy else reference - price
    return float(diff / reference * 10_000)


async def round_trips(
    session: AsyncSession, market_id: int, strategy: StrategyConfig
) -> list[RoundTrip]:
    fills = list(
        (
            await session.execute(
                select(FillRecord)
                .where(FillRecord.market_id == market_id)
                .order_by(FillRecord.time, FillRecord.tid)
            )
        ).scalars()
    )
    orders = {
        o.id: o
        for o in (
            await session.execute(select(OrderRecord).where(OrderRecord.market_id == market_id))
        ).scalars()
    }
    funding = list((await session.execute(select(FundingEventRecord))).scalars())
    signals = {
        s.execution_order_id: s
        for s in (
            await session.execute(
                select(SignalRecord).where(SignalRecord.execution_order_id.is_not(None))
            )
        ).scalars()
    }

    episodes: list[list[FillRecord]] = []
    current: list[FillRecord] = []
    for f in fills:
        signed = f.size if f.side == "buy" else -f.size
        if f.start_position == 0 and current:
            episodes.append(current)  # пропущенное закрытие — начинаем новый эпизод
            current = []
        current.append(f)
        if f.start_position + signed == 0:
            episodes.append(current)
            current = []
    if current:
        episodes.append(current)

    trips = []
    for episode in episodes:
        first = episode[0]
        long = first.side == "buy"
        opening = [f for f in episode if (f.side == "buy") == long]
        closing = [f for f in episode if (f.side == "buy") != long]
        closed = sum(f.size for f in closing) == sum(f.size for f in opening)
        opened_at, closed_at = first.time, (closing[-1].time if closed and closing else None)
        trip = RoundTrip(
            side="long" if long else "short",
            opened_at=opened_at,
            closed_at=closed_at,
            size=sum((f.size for f in opening), Decimal(0)),
            entry_price=_vwap(opening),
            exit_price=_vwap(closing) if closing else None,
            fees=sum((f.fee for f in episode), Decimal(0)),
            funding=sum(
                (
                    e.usdc
                    for e in funding
                    if e.coin == strategy.market.coin
                    and e.time >= opened_at
                    and (closed_at is None or e.time <= closed_at)
                ),
                Decimal(0),
            ),
            realized_pnl=sum((f.closed_pnl for f in episode), Decimal(0)),
            exit_kind=None,
            bot_entry=first.order_id is not None,
        )

        entry_order = orders.get(first.order_id) if first.order_id else None
        if entry_order is None:
            trip.notes.append("вход не ботом (ручная сделка на бирже)")
        else:
            intent = entry_order.intent or {}
            planned_stop = intent.get("stop_trigger_planned")
            risk_amount = intent.get("risk_amount")
            mid = intent.get("mid")
            if mid:
                trip.entry_slippage_vs_preview_bps = _bps(trip.entry_price, Decimal(mid), buy=long)
            if planned_stop and risk_amount:
                loss = trip.size * abs(trip.entry_price - Decimal(planned_stop))
                # допуск: стоп ставится от фактической цены входа, не от превью
                if loss > Decimal(risk_amount) * Decimal("1.2"):
                    trip.checks.append(
                        f"убыток до стопа {loss:.4f} USDC больше риска {Decimal(risk_amount):.4f}"
                    )
            if trip.notional > strategy.limits.max_position_notional_usdc * Decimal("1.01"):
                trip.checks.append(
                    f"номинал {trip.notional:.2f} больше лимита "
                    f"{strategy.limits.max_position_notional_usdc}"
                )
            stops = [
                o
                for o in orders.values()
                if o.purpose == OrderPurpose.STOP.value and o.parent_order_id == entry_order.id
            ]
            if not stops:
                trip.checks.append("защитный стоп не найден")
            elif max(o.size for o in stops) < trip.size:
                trip.checks.append(
                    f"стоп на {max(o.size for o in stops)} меньше позиции {trip.size}"
                )
            signal = signals.get(entry_order.id)
            if signal is not None:
                trip.signal_decision_time = signal.decision_time
                if signal.reference_price is not None:
                    trip.entry_slippage_vs_signal_bps = _bps(
                        trip.entry_price, signal.reference_price, buy=long
                    )

        if closing:
            last_order = orders.get(closing[-1].order_id) if closing[-1].order_id else None
            if last_order is None:
                trip.exit_kind = "manual"
            else:
                trip.exit_kind = last_order.purpose
        if not closed:
            trip.notes.append("позиция открыта")
        trips.append(trip)
    return trips
