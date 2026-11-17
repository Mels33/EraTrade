"""Mainnet shadow (этап 13, §16.4): реальные данные и прогнозы без подписи ордеров.

Процесс работает без ключа API-кошелька. На каждую закрытую свечу:
1. ждёт свечу в БД; позиция — с биржи (только чтение);
2. сигнал active- и shadow-модели (тот же сервис, что и у торгового цикла; если trader уже
   посчитал сигнал на эту свечу — используется он);
3. рыночные условия: bid/ask, спред, цена, по которой реально можно было начать исполнение,
   проскальзывание относительно close свечи решения;
4. причины, по которым risk engine и guard отказали бы во входе (лимиты убытка, число
   сделок, режим и запрет входов, ширина спреда);
5. фактический результат прошлых сигналов, heartbeat `shadow`.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import logfire
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import StrategyConfig
from app.db.models import SignalRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.domain.account import BestBidAsk, Side
from app.execution.guard import ENTRY_MODES
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.market_data.collector import floor_time
from app.risk.engine import RiskEngine
from app.signals.policy import Decision, PolicyParams, PositionContext
from app.signals.service import (
    PredictorCache,
    SignalResult,
    fill_outcomes,
    generate_signal,
    position_context,
)

COMPONENT = "shadow"


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    close_grace: float = 8.0  # чуть позже trader-а: его сигнал (если он запущен) уже сохранён
    candle_wait: float = 45.0
    poll_interval: float = 2.0
    heartbeat_interval: float = 30.0


@dataclass
class ShadowReport:
    decision_time: datetime
    signal: SignalResult | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    risk_reasons: list[str] = field(default_factory=list)
    outcomes_filled: int = 0
    warnings: list[str] = field(default_factory=list)


def executable_price(
    decision: Decision, position: PositionContext, book: BestBidAsk
) -> tuple[Decimal | None, Side | None]:
    """Цена, по которой начиналось бы исполнение (taker): покупка — ask, продажа — bid."""
    if decision is Decision.BUY:
        return book.ask, Side.BUY
    if decision is Decision.SELL:
        return book.bid, Side.SELL
    if decision is Decision.EXIT and position.side is not None:
        return (book.bid, Side.SELL) if position.side is Side.BUY else (book.ask, Side.BUY)
    return None, None


def slippage_bps(price: Decimal, reference: Decimal, side: Side) -> float:
    """Насколько цена исполнения хуже close свечи решения (положительное — хуже)."""
    diff = price - reference if side is Side.BUY else reference - price
    return float(diff / reference * 10_000)


class ShadowObserver:
    def __init__(
        self,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        strategy: StrategyConfig,
        predictors: PredictorCache,
        *,
        network: Network,
        market_id: int,
        coin: str,
        user: str,
        params: PolicyParams,
        interval: str,
        step: timedelta,
        clock: Callable[[], datetime],
        config: ShadowConfig = ShadowConfig(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.gateway = gateway
        self.sm = sessionmaker
        self.strategy = strategy
        self.predictors = predictors
        self.network = network
        self.market_id = market_id
        self.coin = coin
        self.user = user
        self.params = params
        self.interval = interval
        self.step = step
        self.clock = clock
        self.config = config
        self._sleep = sleep
        self.risk = RiskEngine(
            gateway,
            sessionmaker,
            strategy,
            market_id=market_id,
            coin=coin,
            user=user,
            network=network,
            clock=clock,
        )
        self.instance_id = uuid.uuid4().hex[:12]
        self.started_at = clock()
        self.last_report: ShadowReport | None = None

    async def wait_for_candle(self, decision_time: datetime) -> bool:
        waited = 0.0
        while True:
            async with self.sm() as session:
                last = await market_repo.get_last_closed_candle(
                    session, self.market_id, self.interval
                )
            if last is not None and last.close_time >= decision_time:
                return True
            if waited >= self.config.candle_wait:
                return False
            await self._sleep(self.config.poll_interval)
            waited += self.config.poll_interval

    async def risk_reasons(self, book: BestBidAsk) -> list[str]:
        """Почему вход был бы отклонён прямо сейчас (risk engine + условия guard)."""
        reasons = self.risk.entry_blocks(await self.risk.snapshot())
        async with self.sm() as session:
            state = await control.get_bot_state(session)
        if state is None:
            reasons.append("состояние бота не инициализировано")
        else:
            mode = TradingMode(state.trading_mode)
            if mode not in ENTRY_MODES[self.network]:
                reasons.append(f"режим {mode.value} не разрешает новые входы")
            if state.entries_blocked:
                reasons.append(f"входы запрещены: {state.block_reason}")
        if book.spread_bps > self.strategy.limits.max_spread_bps:
            reasons.append(
                f"спред {book.spread_bps:.2f} bps шире лимита {self.strategy.limits.max_spread_bps}"
            )
        return reasons

    async def run_once(self, decision_time: datetime | None = None) -> ShadowReport:
        decision_time = decision_time or floor_time(self.clock(), self.step)
        report = ShadowReport(decision_time=decision_time)
        with logfire.span("shadow {decision_time}", decision_time=decision_time.isoformat()):
            if not await self.wait_for_candle(decision_time):
                report.warnings.append("закрытая свеча не появилась в БД вовремя")
            state = await self.gateway.get_account_state(self.user)
            pos = state.position(self.coin)
            side = (
                None if pos is None or pos.size == 0 else (Side.BUY if pos.size > 0 else Side.SELL)
            )
            async with self.sm.begin() as session:
                position = await position_context(session, self.market_id, side)
                report.signal = await generate_signal(
                    session,
                    market_id=self.market_id,
                    interval=self.interval,
                    step=self.step,
                    params=self.params,
                    position=position,
                    predictors=self.predictors,
                    decision_time=decision_time,
                    now=self.clock(),
                    max_delay=timedelta(seconds=self.strategy.freshness.max_decision_delay_seconds),
                )

            book = await self.gateway.get_best_bid_ask(self.coin)
            report.bid, report.ask = book.bid, book.ask
            try:
                report.risk_reasons = await self.risk_reasons(book)
            except Exception as exc:
                logfire.exception("shadow: проверка risk не выполнена")
                report.risk_reasons = [f"risk не проверен: {exc}"]

            async with self.sm.begin() as session:
                candle = next(
                    iter(
                        await market_repo.get_candles(
                            session,
                            self.market_id,
                            self.interval,
                            start=decision_time - self.step,
                            end=decision_time,
                        )
                    ),
                    None,
                )
                reference = candle.close if candle is not None else None
                for d in report.signal.decisions:
                    if d.signal_id is None:
                        continue
                    price, exec_side = executable_price(d.decision, position, book)
                    values: dict[str, Any] = {
                        "market_bid": book.bid,
                        "market_ask": book.ask,
                        "spread_bps": float(book.spread_bps),
                        "reference_price": reference,
                        "executable_price": price,
                        "entry_slippage_bps": (
                            slippage_bps(price, reference, exec_side)
                            if price is not None and reference is not None and exec_side
                            else None
                        ),
                        "risk_reasons": report.risk_reasons
                        if d.decision in (Decision.BUY, Decision.SELL)
                        else [],
                        "conditions_at": self.clock(),
                    }
                    await session.execute(
                        update(SignalRecord)
                        .where(SignalRecord.id == d.signal_id, SignalRecord.conditions_at.is_(None))
                        .values(**values)
                    )
                report.outcomes_filled = await fill_outcomes(
                    session,
                    market_id=self.market_id,
                    interval=self.interval,
                    step=self.step,
                    threshold_bps=self.strategy.target.threshold_bps,
                    now=self.clock(),
                )
        self.last_report = report
        await self.heartbeat()
        return report

    async def heartbeat(self) -> None:
        last = self.last_report
        details: dict[str, Any] = {"last_decision_time": None, "decisions": None}
        if last is not None and last.signal is not None:
            details = {
                "last_decision_time": last.decision_time.isoformat(),
                "decisions": {d.role: d.decision.value for d in last.signal.decisions},
                "feature_error": last.signal.feature_error,
            }
        async with self.sm.begin() as session:
            await control.beat(
                session,
                component=COMPONENT,
                instance_id=self.instance_id,
                started_at=self.started_at,
                now=self.clock(),
                details=details,
            )

    async def run_forever(self, on_report: Callable[[ShadowReport], None] | None = None) -> None:
        async def beats() -> None:
            while True:
                try:
                    await self.heartbeat()
                except Exception:
                    logfire.exception("shadow: heartbeat не записан")
                await self._sleep(self.config.heartbeat_interval)

        async def cycles() -> None:
            while True:
                now = self.clock()
                boundary = floor_time(now, self.step) + self.step
                await self._sleep((boundary - now).total_seconds() + self.config.close_grace)
                try:
                    report = await self.run_once(boundary)
                except Exception:
                    logfire.exception("shadow: проход не завершён")
                    continue
                if on_report is not None:
                    on_report(report)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(beats())
            tg.create_task(cycles())
