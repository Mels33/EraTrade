"""Торговый цикл (этап 12): закрытая свеча → сигнал → политика → risk/guard → исполнение.

Один проход на закрытую свечу:
1. дождаться, пока коллектор сохранит закрытую свечу (иначе признаки дадут HOLD);
2. сверка перед сделкой: критичное расхождение запрещает входы (выходы разрешены);
3. позиция — с биржи (источник истины), время входа/выхода — по исполнениям в БД;
4. сигнал active- и shadow-модели (сохраняется, один на свечу и версию);
5. исполнение только active-сигнала: сначала атомарный claim сигнала в БД, затем биржа.
   Повтор прохода, рестарт или второй вызов не исполнят сигнал ещё раз — даже если процесс
   упал посреди отправки (статус executing остаётся, ордер разберут tracker и сверка);
6. фактический результат прошлых сигналов, heartbeat.

Режим и лимиты проверяет guard/risk engine внутри ExecutionEngine: в LIVE_PAUSED и при
сработавшем лимите входы блокируются, закрытие разрешено. Решение старше
max_decision_delay не исполняется.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import logfire

from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.db.repositories import models as models_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.domain.orders import OrderStatus
from app.execution import emergency
from app.execution.engine import Confirm, ExecutionEngine, TradeResult
from app.live.review import round_trips
from app.market_data.collector import floor_time
from app.reconciliation.reconciler import Reconciler, ReconciliationReport, Severity
from app.signals.policy import Decision, PolicyParams
from app.signals.service import (
    PredictorCache,
    SignalResult,
    fill_outcomes,
    generate_signal,
    position_context,
)

COMPONENT = "trader"
AUTO_ACTOR = Actor(ActorKind.SYSTEM, "auto-trader")
OPERATOR_DECLINED = "оператор отклонил ордер"


@dataclass(frozen=True, slots=True)
class CycleConfig:
    close_grace: float = 5.0  # секунд после границы свечи до начала прохода
    candle_wait: float = 45.0  # сколько ждать закрытую свечу в БД
    poll_interval: float = 2.0
    heartbeat_interval: float = 30.0


@dataclass
class CycleReport:
    decision_time: datetime
    signal: SignalResult | None = None
    execution_status: str | None = None
    execution_detail: str = ""
    reconciliation_critical: bool = False
    outcomes_filled: int = 0
    warnings: list[str] = field(default_factory=list)
    paused: str | None = None  # причина автопаузы (режим auto)


def engine_reconciler(engine: ExecutionEngine) -> Reconciler:
    return Reconciler(
        engine.gateway,
        engine.sm,
        engine.tracker,
        network=engine.network,
        market_id=engine.market_id,
        coin=engine.coin,
        user=engine.user,
        clock=engine.clock,
        risk=engine.risk,
        notifier=engine.notifier,
    )


def classify(result: TradeResult) -> tuple[str, str, uuid.UUID | None]:
    """TradeResult → (execution_status, описание, id ордера)."""
    order = result.order
    warnings = "; ".join(result.warnings)
    if order is None:
        return "skipped", warnings or "ордер не создавался", None
    detail = f"{order.status.value} {order.filled_size}"
    if order.avg_price is not None:
        detail += f" @ {order.avg_price}"
    if order.guard_reasons:
        detail += f" ({'; '.join(order.guard_reasons)})"
    if order.error:
        detail += f" [{order.error}]"
    if warnings:
        detail += f"; {warnings}"
    if order.status is OrderStatus.BLOCKED:
        status = "declined" if OPERATOR_DECLINED in order.guard_reasons else "blocked"
        return status, detail, order.order_id
    if order.status is OrderStatus.UNKNOWN:
        return "error", f"исход неизвестен, разбирает tracker: {detail}", order.order_id
    return "executed", detail, order.order_id


class TradingCycle:
    def __init__(
        self,
        engine: ExecutionEngine,
        predictors: PredictorCache,
        *,
        params: PolicyParams,
        interval: str,
        step: timedelta,
        max_delay: timedelta,
        confirm: Confirm | None,
        reconcile: Callable[[], Awaitable[ReconciliationReport]] | None = None,
        config: CycleConfig = CycleConfig(),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        auto: bool = False,
    ) -> None:
        self.engine = engine
        self.auto = auto
        self.predictors = predictors
        self.params = params
        self.interval = interval
        self.step = step
        self.max_delay = max_delay
        self.confirm = confirm
        self.config = config
        self._sleep = sleep
        if reconcile is None:
            reconciler = engine_reconciler(engine)

            async def reconcile() -> ReconciliationReport:
                return await reconciler.run("pre_trade")

        self._reconcile = reconcile
        self.clock = engine.clock
        self.instance_id = uuid.uuid4().hex[:12]
        self.started_at = self.clock()
        self.last_report: CycleReport | None = None
        self._reviewed_until = self.started_at  # сделки, закрытые раньше, уже разобраны

    # --- автопауза (этап 15) ---

    async def _auto_pause(self, report: CycleReport, reason: str) -> None:
        """В режиме auto любая необъяснимая ситуация останавливает новые входы до оператора."""
        if not self.auto or report.paused:
            return
        report.paused = reason
        async with self.engine.sm() as session:
            state = await control.get_bot_state(session)
        if state is not None and state.entries_blocked:
            return  # уже на паузе — не дублируем уведомление каждый час
        await emergency.pause(
            self.engine.sm,
            self.engine.network,
            actor=AUTO_ACTOR,
            reason=f"автопауза: {reason}",
            notifier=self.engine.notifier,
        )

    async def _review_closed_trades(self, report: CycleReport) -> None:
        async with self.engine.sm() as session:
            trips = await round_trips(session, self.engine.market_id, self.engine.strategy)
        fresh = [
            t
            for t in trips
            if t.bot_entry and t.closed_at is not None and t.closed_at > self._reviewed_until
        ]
        for trip in fresh:
            if trip.checks:
                await self._auto_pause(
                    report, f"разбор сделки {trip.opened_at:%m-%d %H:%M}: {'; '.join(trip.checks)}"
                )
        if fresh:
            self._reviewed_until = max(t.closed_at for t in fresh if t.closed_at)

    # --- шаги ---

    async def wait_for_candle(self, decision_time: datetime) -> bool:
        deadline = self.config.candle_wait
        waited = 0.0
        while True:
            async with self.engine.sm() as session:
                last = await market_repo.get_last_closed_candle(
                    session, self.engine.market_id, self.interval
                )
            if last is not None and last.close_time >= decision_time:
                return True
            if waited >= deadline:
                return False
            await self._sleep(self.config.poll_interval)
            waited += self.config.poll_interval

    async def _position_side(self) -> Side | None:
        state = await self.engine.gateway.get_account_state(self.engine.user)
        position = state.position(self.engine.coin)
        if position is None or position.size == 0:
            return None
        return Side.BUY if position.size > 0 else Side.SELL

    async def _finish(
        self,
        report: CycleReport,
        signal_id: int,
        status: str,
        detail: str,
        order_id: uuid.UUID | None = None,
    ) -> None:
        async with self.engine.sm.begin() as session:
            await models_repo.finish_execution(
                session,
                signal_id,
                status=status,
                detail=detail,
                now=self.clock(),
                order_id=order_id,
            )
        report.execution_status, report.execution_detail = status, detail

    async def _execute(self, report: CycleReport, signal_id: int, decision: Decision) -> None:
        if decision is Decision.HOLD:
            async with self.engine.sm.begin() as session:
                if await models_repo.claim_execution(session, signal_id, self.clock()):
                    await models_repo.finish_execution(
                        session, signal_id, status="none", detail="HOLD", now=self.clock()
                    )
            report.execution_status, report.execution_detail = "none", "HOLD"
            return

        async with self.engine.sm.begin() as session:
            claimed = await models_repo.claim_execution(session, signal_id, self.clock())
        if not claimed:
            report.execution_status = "already_handled"
            report.execution_detail = "сигнал уже обрабатывался — повторно не исполняется"
            return

        age = self.clock() - report.decision_time
        if age > self.max_delay:
            await self._finish(
                report,
                signal_id,
                "skipped",
                f"опоздание {age.total_seconds():.0f} с — не исполняем",
            )
            return
        if decision in (Decision.BUY, Decision.SELL) and report.reconciliation_critical:
            await self._finish(
                report, signal_id, "skipped", "вход запрещён: критичные расхождения сверки"
            )
            return
        try:
            if decision is Decision.EXIT:
                result = await self.engine.close_position(confirm=self.confirm)
            else:
                side = Side.BUY if decision is Decision.BUY else Side.SELL
                result = await self.engine.open_position(side, None, confirm=self.confirm)
        except Exception as exc:  # без повтора: статус error, разбирают tracker и оператор
            logfire.exception("trader: ошибка исполнения сигнала {signal_id}", signal_id=signal_id)
            await self._finish(report, signal_id, "error", f"{type(exc).__name__}: {exc}")
            await self.engine.notifier.send(
                f"EraTrade: ошибка исполнения {decision.value} на {report.decision_time:%H:%M}: "
                f"{exc}"
            )
            return
        status, detail, order_id = classify(result)
        await self._finish(report, signal_id, status, detail, order_id)
        if status in ("executed", "error"):
            await self.engine.notifier.send(
                f"EraTrade: {decision.value} на {report.decision_time:%Y-%m-%d %H:%M} — {detail}"
            )

    async def heartbeat(self) -> None:
        now = self.clock()
        last = self.last_report
        details: dict[str, Any] = {
            "last_decision_time": last.decision_time.isoformat() if last else None,
            "last_execution": last.execution_status if last else None,
            "auto": self.auto,
            "paused": last.paused if last else None,
        }
        async with self.engine.sm.begin() as session:
            await control.beat(
                session,
                component=COMPONENT,
                instance_id=self.instance_id,
                started_at=self.started_at,
                now=now,
                details=details,
            )

    # --- проход ---

    async def run_once(self, decision_time: datetime | None = None) -> CycleReport:
        decision_time = decision_time or floor_time(self.clock(), self.step)
        report = CycleReport(decision_time=decision_time)
        with logfire.span("trader cycle {decision_time}", decision_time=decision_time.isoformat()):
            if not await self.wait_for_candle(decision_time):
                report.warnings.append("закрытая свеча не появилась в БД вовремя")
            try:
                reconciliation = await self._reconcile()
                report.reconciliation_critical = bool(reconciliation.critical)
                unexplained = [
                    d
                    for d in reconciliation.differences
                    if not d.repaired
                    and (
                        d.severity is Severity.CRITICAL
                        or (
                            d.severity is Severity.WARNING
                            and self.engine.strategy.auto.pause_on_warnings
                        )
                    )
                ]
                if unexplained:
                    await self._auto_pause(
                        report, "сверка: " + "; ".join(d.message for d in unexplained)
                    )
            except Exception as exc:
                logfire.exception("trader: сверка не выполнена")
                report.reconciliation_critical = True  # без сверки входить нельзя
                report.warnings.append(f"сверка не выполнена: {exc}")
                await self._auto_pause(report, f"сверка не выполнена: {exc}")

            side = await self._position_side()
            async with self.engine.sm.begin() as session:
                position = await position_context(session, self.engine.market_id, side)
                report.signal = await generate_signal(
                    session,
                    market_id=self.engine.market_id,
                    interval=self.interval,
                    step=self.step,
                    params=self.params,
                    position=position,
                    predictors=self.predictors,
                    decision_time=decision_time,
                    now=self.clock(),
                    max_delay=self.max_delay,
                )

            active = report.signal.active
            if active is None:
                report.execution_status = "none"
                report.execution_detail = "нет активной модели — только shadow-сигналы"
            elif active.signal_id is not None:
                await self._execute(report, active.signal_id, active.decision)
                if report.execution_status == "error":
                    await self._auto_pause(report, f"ошибка исполнения: {report.execution_detail}")
            await self._review_closed_trades(report)

            async with self.engine.sm.begin() as session:
                report.outcomes_filled = await fill_outcomes(
                    session,
                    market_id=self.engine.market_id,
                    interval=self.interval,
                    step=self.step,
                    threshold_bps=self.engine.strategy.target.threshold_bps,
                    now=self.clock(),
                )
        self.last_report = report
        await self.heartbeat()
        return report

    async def start(self) -> bool:
        """Старт процесса цикла: LIVE_ENABLED прошлого запуска понижается до LIVE_PAUSED."""
        async with self.engine.sm.begin() as session:
            downgraded = await control.downgrade_after_restart(session, self.engine.network)
        if downgraded:
            await self.engine.notifier.send(
                "EraTrade: торговый цикл перезапущен — режим понижен до LIVE_PAUSED. "
                "Включение торговли — вручную (eratrade set-mode LIVE_ENABLED)."
            )
        return downgraded

    async def run_forever(self, on_report: Callable[[CycleReport], None] | None = None) -> None:
        await self.start()

        async def beats() -> None:
            while True:
                try:
                    await self.heartbeat()
                except Exception:
                    logfire.exception("trader: heartbeat не записан")
                await self._sleep(self.config.heartbeat_interval)

        async def cycles() -> None:
            while True:
                now = self.clock()
                boundary = floor_time(now, self.step) + self.step
                wait = (boundary - now).total_seconds() + self.config.close_grace
                await self._sleep(wait)
                try:
                    report = await self.run_once(boundary)
                except Exception as exc:
                    logfire.exception("trader: проход не завершён")
                    await self.engine.notifier.send(
                        f"EraTrade: торговый цикл упал на проходе: {exc}"
                    )
                    continue
                if on_report is not None:
                    on_report(report)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(beats())
            tg.create_task(cycles())
