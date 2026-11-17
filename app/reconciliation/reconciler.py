"""Сверка локального состояния с биржей (§11 плана). Биржа — источник истины.

Не требует ключа API-кошелька: только чтение биржи и запись в БД. Поэтому может работать
отдельным процессом рядом с торговым (минимальные права).

Порядок прогона:
 1. прерванные до подписи намерения → BLOCKED (на биржу физически не уходили);
 2. незавершённые ордера → уточнение по cloid (orderStatus, fills);
 3. догрузка пропущенных fills, funding и ledger;
 4. ожидаемая позиция (baseline + fills ордеров бота) против позиции на бирже;
 5. исполнения и ордера не от бота;
 6. защита: на открытую позицию должен стоять reduceOnly-стоп не меньшего размера.
Необъяснимое критичное расхождение → запрет новых входов и LIVE_ENABLED → LIVE_PAUSED.
"""

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import StrategyConfig
from app.db.models import FundingEventRecord, LedgerEventRecord
from app.db.repositories import control
from app.db.repositories import reconciliation as rec_repo
from app.db.repositories import trading as trading_repo
from app.domain.account import Side
from app.domain.control import Actor, ActorKind
from app.domain.orders import OrderStatus
from app.execution.tracker import OrderTracker
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.market_data.metadata import sync_market
from app.observability.notify import LogNotifier, Notifier
from app.risk.engine import RiskEngine

RECONCILER_ACTOR = Actor(ActorKind.SYSTEM, "reconciler")
COMPONENT = "reconciler"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class DiffKind(StrEnum):
    ABANDONED_INTENT = "abandoned_intent"
    ORDER_RESOLVED = "order_resolved"
    ORDER_STILL_UNKNOWN = "order_still_unknown"
    MISSED_FILLS = "missed_fills"
    ACCOUNT_EVENTS = "account_events"
    FOREIGN_FILLS = "foreign_fills"
    POSITION_MISMATCH = "position_mismatch"
    FOREIGN_ORDER = "foreign_order"
    LOCAL_STOP_MISSING = "local_stop_missing"
    UNPROTECTED_POSITION = "unprotected_position"
    UNDERSIZED_STOP = "undersized_stop"
    LOSS_LIMIT = "loss_limit"
    ADDRESS_RATE_LIMIT = "address_rate_limit"


@dataclass(frozen=True, slots=True)
class Difference:
    kind: DiffKind
    severity: Severity
    message: str
    repaired: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "repaired": self.repaired,
            "message": self.message,
            "details": self.details,
        }


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    run_id: int
    status: str  # ok, warnings, critical
    differences: tuple[Difference, ...]
    exchange_position: Decimal
    expected_position: Decimal
    entries_blocked: bool

    @property
    def critical(self) -> list[Difference]:
        return [d for d in self.differences if d.severity is Severity.CRITICAL and not d.repaired]


class Reconciler:
    def __init__(
        self,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        tracker: OrderTracker,
        *,
        network: Network,
        market_id: int,
        coin: str,
        user: str,
        clock: Callable[[], datetime] = _utc_now,
        fill_lookback: timedelta = timedelta(hours=24),
        events_lookback: timedelta = timedelta(days=7),
        abandon_after: timedelta = timedelta(minutes=5),
        risk: RiskEngine | None = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.gateway = gateway
        self.sm = sessionmaker
        self.tracker = tracker
        self.network = network
        self.market_id = market_id
        self.coin = coin
        self.user = user
        self.clock = clock
        self.fill_lookback = fill_lookback
        self.events_lookback = events_lookback
        self.abandon_after = abandon_after
        self.risk = risk
        self.notifier = notifier or LogNotifier()
        self.instance_id = uuid.uuid4().hex[:12]
        self.started_at = clock()

    @classmethod
    async def create(
        cls,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        strategy: StrategyConfig,
        *,
        user: str,
        notifier: Notifier | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "Reconciler":
        async with sessionmaker.begin() as db:
            market_id, meta, _ = await sync_market(gateway, db, strategy.market.coin)
            await rec_repo.mark_abandoned_runs(db, market_id, clock())
        tracker = OrderTracker(
            gateway,
            sessionmaker,
            market_id=market_id,
            coin=meta.coin,
            user=user,
            max_unknown_seconds=strategy.freshness.max_unknown_order_seconds,
            clock=clock,
        )
        risk = RiskEngine(
            gateway,
            sessionmaker,
            strategy,
            market_id=market_id,
            coin=meta.coin,
            user=user,
            network=gateway.network,
            notifier=notifier,
            clock=clock,
        )
        return cls(
            gateway,
            sessionmaker,
            tracker,
            network=gateway.network,
            market_id=market_id,
            coin=meta.coin,
            user=user,
            clock=clock,
            risk=risk,
            notifier=notifier,
        )

    async def run(self, trigger: str) -> ReconciliationReport:
        async with self.sm.begin() as db:
            run_id = (await rec_repo.start_run(db, self.market_id, trigger)).id
        with logfire.span("reconcile {trigger}", trigger=trigger, run_id=run_id):
            try:
                report = await self._run(run_id)
            except Exception as exc:
                async with self.sm.begin() as db:
                    await rec_repo.finish_run(
                        db,
                        run_id,
                        status="error",
                        finished_at=self.clock(),
                        summary={"error": f"{type(exc).__name__}: {exc}"[:500]},
                        differences=[],
                    )
                raise
        level = {"ok": "info", "warnings": "warn", "critical": "error"}[report.status]
        logfire.log(
            level,  # type: ignore[arg-type]
            "reconcile: {status}, расхождений {count}",
            attributes={"status": report.status, "count": len(report.differences)},
        )
        return report

    async def _run(self, run_id: int) -> ReconciliationReport:
        diffs: list[Difference] = []

        # 1. Намерения, прерванные до подписи.
        for result in await self.tracker.abandon_stale_intents(self.abandon_after):
            diffs.append(
                Difference(
                    DiffKind.ABANDONED_INTENT,
                    Severity.INFO,
                    f"намерение {result.cloid} прервано до подписи → BLOCKED",
                    repaired=True,
                    details={"cloid": result.cloid},
                )
            )

        # 2. Незавершённые ордера — по данным биржи.
        async with self.sm() as db:
            before = {
                o.id: o.status
                for o in await trading_repo.list_orders(
                    db, statuses=list(trading_repo.UNRESOLVED), market_id=self.market_id, limit=200
                )
            }
        for result in await self.tracker.resolve_unresolved():
            old = before.get(result.order_id)
            if old is not None and old != result.status.value:
                diffs.append(
                    Difference(
                        DiffKind.ORDER_RESOLVED,
                        Severity.INFO,
                        f"ордер {result.cloid}: {old} → {result.status.value}",
                        repaired=True,
                        details={"cloid": result.cloid, "from": old, "to": result.status.value},
                    )
                )
            if result.status in (OrderStatus.SIGNED, OrderStatus.UNKNOWN):
                diffs.append(
                    Difference(
                        DiffKind.ORDER_STILL_UNKNOWN,
                        Severity.WARNING,
                        f"ордер {result.cloid}: исход пока неизвестен",
                        details={"cloid": result.cloid},
                    )
                )

        # 3. Пропущенные fills, funding, ledger.
        now = self.clock()
        async with self.sm() as db:
            last_fill = await rec_repo.last_fill_time(db, self.market_id)
            last_funding = await rec_repo.last_event_time(db, FundingEventRecord)
            last_ledger = await rec_repo.last_event_time(db, LedgerEventRecord)
        fills_since = last_fill - timedelta(hours=1) if last_fill else now - self.fill_lookback
        new_fills = await self.tracker.sync_fills(fills_since)
        if new_fills:
            diffs.append(
                Difference(
                    DiffKind.MISSED_FILLS,
                    Severity.INFO,
                    f"догружено исполнений: {new_fills}",
                    repaired=True,
                    details={"count": new_fills},
                )
            )
        funding = await self.gateway.get_funding(
            self.user,
            (last_funding - timedelta(hours=1)) if last_funding else now - self.events_lookback,
        )
        ledger = await self.gateway.get_ledger(
            self.user,
            (last_ledger - timedelta(hours=1)) if last_ledger else now - self.events_lookback,
        )
        async with self.sm.begin() as db:
            new_funding = await rec_repo.add_funding_events(db, funding)
            new_ledger = await rec_repo.add_ledger_events(db, ledger)
        if new_funding or new_ledger:
            diffs.append(
                Difference(
                    DiffKind.ACCOUNT_EVENTS,
                    Severity.INFO,
                    f"новые события счёта: funding {new_funding}, ledger {new_ledger}",
                    repaired=True,
                    details={"funding": new_funding, "ledger": new_ledger},
                )
            )

        # 4–5. Позиция и ордера на бирже.
        state = await self.gateway.get_account_state(self.user)
        remote_position = state.position(self.coin)
        exchange_position = remote_position.size if remote_position else Decimal(0)
        open_orders = [
            o for o in await self.gateway.get_open_orders(self.user) if o.coin == self.coin
        ]

        async with self.sm() as db:
            expected, baseline_at = await rec_repo.expected_position(db, self.market_id)
            foreign = await rec_repo.foreign_fills(db, self.market_id, baseline_at)
            local_active = await trading_repo.list_orders(
                db, statuses=[OrderStatus.RESTING, OrderStatus.TRIGGERED], market_id=self.market_id
            )
            known_cloids = {
                o.cloid
                for o in await trading_repo.list_orders(db, market_id=self.market_id, limit=1000)
            }

        if foreign:
            diffs.append(
                Difference(
                    DiffKind.FOREIGN_FILLS,
                    Severity.WARNING,
                    f"исполнения не по ордерам бота: {len(foreign)} "
                    "(ручная торговля или ликвидация)",
                    details={"tids": [f.tid for f in foreign][:50]},
                )
            )
        if expected != exchange_position:
            diffs.append(
                Difference(
                    DiffKind.POSITION_MISMATCH,
                    Severity.CRITICAL,
                    f"позиция на бирже {exchange_position}, ожидаемая ботом {expected}",
                    details={
                        "exchange": str(exchange_position),
                        "expected": str(expected),
                        "foreign_fills": len(foreign),
                    },
                )
            )

        for order in open_orders:
            if order.cloid is None or order.cloid not in known_cloids:
                diffs.append(
                    Difference(
                        DiffKind.FOREIGN_ORDER,
                        Severity.WARNING,
                        f"ордер на бирже не создан ботом: oid {order.oid} {order.order_type} "
                        f"{order.side.value} {order.size}",
                        details={"oid": order.oid, "cloid": order.cloid},
                    )
                )
        remote_cloids = {o.cloid for o in open_orders if o.cloid}
        for local in local_active:
            if local.cloid not in remote_cloids:
                diffs.append(
                    Difference(
                        DiffKind.LOCAL_STOP_MISSING,
                        Severity.WARNING,
                        f"ордер {local.cloid} {local.status} в БД, но не найден среди открытых",
                        details={"cloid": local.cloid},
                    )
                )

        # 6. Защита открытой позиции.
        if exchange_position != 0:
            closing_side = Side.SELL if exchange_position > 0 else Side.BUY
            covered = sum(
                (
                    o.size
                    for o in open_orders
                    if o.is_trigger and o.reduce_only and o.side is closing_side
                ),
                Decimal(0),
            )
            if covered == 0:
                diffs.append(
                    Difference(
                        DiffKind.UNPROTECTED_POSITION,
                        Severity.CRITICAL,
                        f"позиция {exchange_position} без биржевого стопа",
                        details={"position": str(exchange_position)},
                    )
                )
            elif covered < abs(exchange_position):
                diffs.append(
                    Difference(
                        DiffKind.UNDERSIZED_STOP,
                        Severity.CRITICAL,
                        f"стопы покрывают {covered} из {abs(exchange_position)}",
                        details={"covered": str(covered), "position": str(exchange_position)},
                    )
                )

        # 7. Лимиты убытка (в т. ч. нереализованный убыток) и лимит запросов адреса.
        if self.risk is not None:
            snap = await self.risk.snapshot()
            for breach in self.risk.loss_breaches(snap):
                diffs.append(
                    Difference(
                        DiffKind.LOSS_LIMIT,
                        Severity.CRITICAL,
                        breach,
                        details={
                            "daily_pnl": str(snap.daily_pnl),
                            "weekly_pnl": str(snap.weekly_pnl),
                            "equity": str(snap.equity),
                        },
                    )
                )
            limits = await self.gateway.get_user_rate_limit(self.user)
            if limits.remaining < self.risk.strategy.risk.address_requests_warning:
                diffs.append(
                    Difference(
                        DiffKind.ADDRESS_RATE_LIMIT,
                        Severity.WARNING,
                        f"у адреса осталось {limits.remaining} запросов",
                        details={"remaining": limits.remaining},
                    )
                )

        await self.tracker.snapshot()
        return await self._finish(run_id, diffs, exchange_position, expected)

    async def _finish(
        self,
        run_id: int,
        diffs: list[Difference],
        exchange_position: Decimal,
        expected: Decimal,
    ) -> ReconciliationReport:
        critical = [d for d in diffs if d.severity is Severity.CRITICAL and not d.repaired]
        warnings = [d for d in diffs if d.severity is Severity.WARNING]
        status = "critical" if critical else "warnings" if warnings else "ok"
        blocked = False
        newly_blocked = False
        async with self.sm.begin() as db:
            if critical:
                reason = "сверка: " + "; ".join(d.message for d in critical)
                newly_blocked = await control.block_entries(
                    db, self.network, actor=RECONCILER_ACTOR, reason=reason[:500]
                )
                state = await control.get_bot_state(db)
                if state is not None and state.trading_mode == TradingMode.LIVE_ENABLED.value:
                    await control.set_trading_mode(
                        db,
                        self.network,
                        TradingMode.LIVE_PAUSED,
                        actor=RECONCILER_ACTOR,
                        reason=reason[:500],
                    )
            state = await control.get_bot_state(db)
            blocked = bool(state and state.entries_blocked)
            await rec_repo.finish_run(
                db,
                run_id,
                status=status,
                finished_at=self.clock(),
                summary={
                    "exchange_position": str(exchange_position),
                    "expected_position": str(expected),
                    "critical": len(critical),
                    "warnings": len(warnings),
                },
                differences=[d.as_row() for d in diffs],
            )
        if newly_blocked:
            await self.notifier.send(
                "сверка: " + "; ".join(d.message for d in critical) + ". Новые входы заблокированы."
            )
        return ReconciliationReport(
            run_id=run_id,
            status=status,
            differences=tuple(diffs),
            exchange_position=exchange_position,
            expected_position=expected,
            entries_blocked=blocked,
        )

    async def accept_exchange_position(self, *, actor: Actor, reason: str) -> Decimal:
        """Оператор признаёт текущую позицию биржи точкой отсчёта (после ручной сделки)."""
        if actor.kind is not ActorKind.OPERATOR or not reason.strip():
            raise PermissionError("принять позицию может только оператор с указанием причины")
        state = await self.gateway.get_account_state(self.user)
        position = state.position(self.coin)
        size = position.size if position else Decimal(0)
        async with self.sm.begin() as db:
            await rec_repo.add_baseline(
                db,
                self.market_id,
                size=size,
                effective_at=state.exchange_time,
                actor=str(actor),
                reason=reason,
            )
            await control.record_audit(
                db,
                actor=actor,
                action="position.baseline_accepted",
                reason=reason,
                details={"coin": self.coin, "size": str(size)},
            )
        return size

    async def run_forever(self, interval: float) -> None:
        """Периодическая сверка с heartbeat. Ошибка прогона не останавливает цикл."""
        while True:
            details: dict[str, Any]
            try:
                report = await self.run("periodic")
                details = {"status": report.status, "differences": len(report.differences)}
            except Exception as exc:
                logfire.exception("reconcile: прогон завершился ошибкой")
                details = {"status": "error", "error": f"{type(exc).__name__}: {exc}"[:300]}
            async with self.sm.begin() as db:
                await control.beat(
                    db,
                    component=COMPONENT,
                    instance_id=self.instance_id,
                    started_at=self.started_at,
                    now=self.clock(),
                    details=details,
                )
            await asyncio.sleep(interval)
