"""Risk engine (§12 плана): лимиты убытка, сделок и размер позиции от риска.

PnL периода = закрытый PnL − комиссии + funding (+ нереализованный PnL позиции, если
loss_includes_unrealized). Периоды — UTC: день с 00:00, неделя с понедельника 00:00.
Превышение лимита убытка — устойчивый запрет новых входов: автоматически на следующий день
не снимается, только оператором (§12.3). Лимит сделок в день сбрасывается сам с новым днём.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network
from app.config.strategy import StrategyConfig
from app.db.repositories import control
from app.db.repositories import risk as risk_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.domain.instruments import InstrumentRules
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.observability.notify import LogNotifier, Notifier
from app.risk.sizing import SizingResult, size_position

RISK_ACTOR = Actor(ActorKind.SYSTEM, "risk")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def day_start(now: datetime) -> datetime:
    return now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime) -> datetime:
    return day_start(now) - timedelta(days=now.astimezone(UTC).weekday())


@dataclass(frozen=True, slots=True)
class RiskSnapshot:
    as_of: datetime
    equity: Decimal  # spot USDC + perp account value
    spot_usdc: Decimal
    perp_account_value: Decimal
    unrealized_pnl: Decimal
    daily_pnl: Decimal
    weekly_pnl: Decimal
    realized_today: Decimal
    fees_today: Decimal
    funding_today: Decimal
    trades_today: int

    @property
    def daily_base(self) -> Decimal:
        """Оценка equity на начало дня: от неё считается доля убытка."""
        return self.equity - self.daily_pnl

    @property
    def weekly_base(self) -> Decimal:
        return self.equity - self.weekly_pnl


class RiskEngine:
    def __init__(
        self,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        strategy: StrategyConfig,
        *,
        market_id: int,
        coin: str,
        user: str,
        network: Network,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.gateway = gateway
        self.sm = sessionmaker
        self.strategy = strategy
        self.market_id = market_id
        self.coin = coin
        self.user = user
        self.network = network
        self.notifier = notifier or LogNotifier()
        self.clock = clock

    async def snapshot(self) -> RiskSnapshot:
        now = self.clock()
        state = await self.gateway.get_account_state(self.user)
        spot = await self.gateway.get_spot_usdc(self.user)
        position = state.position(self.coin)
        unrealized = position.unrealized_pnl if position else Decimal(0)
        include_unrealized = self.strategy.risk.loss_includes_unrealized
        async with self.sm() as db:
            day_pnl, day_fees = await risk_repo.realized_since(db, day_start(now))
            week_pnl, week_fees = await risk_repo.realized_since(db, week_start(now))
            day_funding = await risk_repo.funding_since(db, day_start(now))
            week_funding = await risk_repo.funding_since(db, week_start(now))
            trades = await risk_repo.entries_since(db, self.market_id, day_start(now))
        open_pnl = unrealized if include_unrealized else Decimal(0)
        return RiskSnapshot(
            as_of=now,
            equity=spot + state.account_value,
            spot_usdc=spot,
            perp_account_value=state.account_value,
            unrealized_pnl=unrealized,
            daily_pnl=day_pnl - day_fees + day_funding + open_pnl,
            weekly_pnl=week_pnl - week_fees + week_funding + open_pnl,
            realized_today=day_pnl,
            fees_today=day_fees,
            funding_today=day_funding,
            trades_today=trades,
        )

    def loss_breaches(self, snap: RiskSnapshot) -> list[str]:
        limits = self.strategy.limits
        breaches = []
        daily_limit = snap.daily_base * limits.max_daily_loss_fraction
        if snap.daily_pnl < 0 and -snap.daily_pnl >= daily_limit:
            breaches.append(
                f"дневной убыток {-snap.daily_pnl:.2f} USDC достиг лимита {daily_limit:.2f} USDC "
                f"({limits.max_daily_loss_fraction * 100}% equity)"
            )
        weekly_limit = snap.weekly_base * limits.max_weekly_loss_fraction
        if snap.weekly_pnl < 0 and -snap.weekly_pnl >= weekly_limit:
            breaches.append(
                f"недельный убыток {-snap.weekly_pnl:.2f} USDC достиг лимита "
                f"{weekly_limit:.2f} USDC "
                f"({limits.max_weekly_loss_fraction * 100}% equity)"
            )
        return breaches

    def entry_blocks(self, snap: RiskSnapshot) -> list[str]:
        blocks = self.loss_breaches(snap)
        if snap.trades_today >= self.strategy.limits.max_trades_per_day:
            blocks.append(
                f"лимит сделок на сегодня исчерпан: {snap.trades_today} из "
                f"{self.strategy.limits.max_trades_per_day}"
            )
        return blocks

    def size(
        self,
        snap: RiskSnapshot,
        *,
        side: Side,
        entry_price: Decimal,
        stop_price: Decimal,
        available_margin: Decimal,
        leverage: int,
        rules: InstrumentRules,
        requested_notional: Decimal | None = None,
    ) -> SizingResult:
        return size_position(
            side=side,
            equity=snap.equity,
            entry_price=entry_price,
            stop_price=stop_price,
            available_margin=available_margin,
            leverage=leverage,
            strategy=self.strategy,
            rules=rules,
            requested_notional=requested_notional,
        )

    async def enforce(self, snap: RiskSnapshot) -> list[str]:
        """При превышении лимита убытка — устойчивый запрет входов, аудит и уведомление."""
        breaches = self.loss_breaches(snap)
        if not breaches:
            return []
        reason = "risk: " + "; ".join(breaches)
        async with self.sm.begin() as db:
            newly = await control.block_entries(db, self.network, actor=RISK_ACTOR, reason=reason)
        logfire.error("risk: {reason}", reason=reason)
        if newly:
            await self.notifier.send(f"{reason}. Новые входы заблокированы до решения оператора.")
        return breaches
