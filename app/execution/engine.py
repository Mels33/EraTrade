"""Execution engine (§10 плана): создание, контроль и восстановление ордеров.

Алгоритм отправки (§10.2):
 1. намерение сохраняется в orders (CREATED) до любых действий;
 2. свежее состояние биржи → нормализация → guard → подтверждение оператора;
 3. APPROVED → nonce + подпись → SIGNED (nonce и действие сохранены) → отправка;
 4. ответ разбирается по каждому ордеру (не только status=ok);
 5. при неизвестном исходе — UNKNOWN и сверка по cloid. Повторная отправка запрещена.

После входа сразу ставится биржевой stop-market (reduceOnly) и проверяется его фактическое
наличие. Если защиту поставить не удалось — новые входы блокируются (§12.4).
"""

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import logfire
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import Network, TradingMode
from app.config.strategy import StrategyConfig
from app.db.models import OrderRecord
from app.db.repositories import control
from app.db.repositories import market as market_repo
from app.db.repositories import trading as repo
from app.domain.account import AccountState, ActiveAssetData, BestBidAsk, Side
from app.domain.control import Actor, ActorKind
from app.domain.indicators import atr
from app.domain.instruments import InstrumentRules, Rounding
from app.domain.orders import (
    OrderKind,
    OrderPurpose,
    OrderStatus,
    PlacementFormatError,
    PlacementKind,
    is_final,
    map_exchange_order_status,
    parse_placement_status,
)
from app.execution.guard import GuardContext, GuardDecision, ProposedOrder, check_order
from app.execution.session import TradingSession
from app.integrations.hyperliquid import actions
from app.integrations.hyperliquid.errors import (
    ExchangeNotSentError,
    ExchangeOutcomeUnknownError,
    HyperliquidFormatError,
    HyperliquidRateLimitError,
    HyperliquidRequestError,
)
from app.integrations.hyperliquid.exchange_client import ExchangeResponse
from app.integrations.hyperliquid.gateway import INTERVALS, HyperliquidGateway
from app.market_data.metadata import sync_market
from app.observability.logging import current_correlation_id

ENGINE_ACTOR = Actor(ActorKind.SYSTEM, "execution")
BPS = Decimal(10_000)


class ExecutionError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OrderPreview:
    """Что увидит оператор перед подтверждением."""

    purpose: OrderPurpose
    side: Side
    size: Decimal
    limit_price: Decimal
    notional: Decimal
    mid_price: Decimal
    spread_bps: Decimal | None
    position_size: Decimal
    leverage_target: int
    available_to_trade: Decimal
    stop_trigger: Decimal | None = None
    max_loss_at_stop: Decimal | None = None


@dataclass(frozen=True, slots=True)
class OrderResult:
    order_id: uuid.UUID
    cloid: str
    status: OrderStatus
    filled_size: Decimal
    avg_price: Decimal | None
    error: str | None = None
    guard_reasons: tuple[str, ...] = ()

    @classmethod
    def of(cls, order: OrderRecord, guard_reasons: tuple[str, ...] = ()) -> "OrderResult":
        return cls(
            order_id=order.id,
            cloid=order.cloid,
            status=OrderStatus(order.status),
            filled_size=order.filled_size,
            avg_price=order.avg_fill_price,
            error=order.error,
            guard_reasons=guard_reasons,
        )


@dataclass
class TradeResult:
    order: OrderResult | None
    stop: OrderResult | None = None
    warnings: list[str] = field(default_factory=list)


Confirm = Callable[[OrderPreview], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class _Snapshot:
    state: AccountState
    asset: ActiveAssetData
    book: BestBidAsk
    mode: TradingMode
    entries_blocked: bool
    block_reason: str | None
    open_entry_orders: int

    @property
    def position_size(self) -> Decimal:
        return Decimal(0) if (p := self.state.position(self.asset.coin)) is None else p.size


class ExecutionEngine:
    def __init__(
        self,
        session: TradingSession,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        network: Network,
        market_id: int,
        rules: InstrumentRules,
        strategy: StrategyConfig,
        user: str,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.session = session
        self.gateway = gateway
        self.sm = sessionmaker
        self.network = network
        self.market_id = market_id
        self.rules = rules
        self.strategy = strategy
        self.user = user
        self.clock = clock
        self.coin = rules.coin

    @classmethod
    async def create(
        cls,
        session: TradingSession,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        strategy: StrategyConfig,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> "ExecutionEngine":
        async with sessionmaker.begin() as db:
            market_id, _, rules = await sync_market(gateway, db, strategy.market.coin)
        return cls(
            session,
            gateway,
            sessionmaker,
            network=gateway.network,
            market_id=market_id,
            rules=rules,
            strategy=strategy,
            user=session.wallet.account,
            clock=clock,
        )

    # --- состояние ---

    async def _snapshot(self) -> _Snapshot:
        state = await self.gateway.get_account_state(self.user)
        asset = await self.gateway.get_active_asset_data(self.user, self.coin)
        book = await self.gateway.get_best_bid_ask(self.coin)
        async with self.sm() as db:
            bot = await control.get_bot_state(db)
            open_entries = (
                await db.execute(
                    select(func.count())
                    .select_from(OrderRecord)
                    .where(
                        OrderRecord.market_id == self.market_id,
                        OrderRecord.purpose == OrderPurpose.ENTRY.value,
                        OrderRecord.status.in_([s.value for s in repo.UNRESOLVED]),
                    )
                )
            ).scalar_one()
        return _Snapshot(
            state=state,
            asset=asset,
            book=book,
            mode=TradingMode(bot.trading_mode) if bot else TradingMode.DISABLED,
            entries_blocked=bool(bot and bot.entries_blocked),
            block_reason=bot.block_reason if bot else None,
            open_entry_orders=int(open_entries),
        )

    def _guard(
        self, order: ProposedOrder, snap: _Snapshot, *, leverage: int | None = None
    ) -> GuardDecision:
        ctx = GuardContext(
            network=self.network,
            mode=snap.mode,
            entries_blocked=snap.entries_blocked,
            block_reason=snap.block_reason,
            position_size=snap.position_size,
            open_entry_orders=snap.open_entry_orders,
            mark_price=snap.asset.mark_price,
            available_to_trade=(
                snap.asset.available_to_trade_buy
                if order.side is Side.BUY
                else snap.asset.available_to_trade_sell
            ),
            leverage=leverage if leverage is not None else snap.asset.leverage,
            spread_bps=snap.book.spread_bps,
        )
        return check_order(order, ctx, self.rules, self.strategy)

    def _ioc_price(self, side: Side, mid: Decimal) -> Decimal:
        """Граница IOC: не хуже mid ± max_slippage, округление в безопасную сторону."""
        slip = self.strategy.limits.max_slippage_bps / BPS
        if side is Side.BUY:
            return self.rules.round_price(mid * (1 + slip), Rounding.DOWN)
        return self.rules.round_price(mid * (1 - slip), Rounding.UP)

    async def stop_distance(self, mid: Decimal) -> Decimal:
        """max(ATR(14) × 1.5, 0.5% цены) по закрытым свечам из БД (spec §4)."""
        sizing = self.strategy.sizing
        step = INTERVALS[self.strategy.market.interval]
        end = self.clock()
        async with self.sm() as db:
            candles = await market_repo.get_candles(
                db,
                self.market_id,
                self.strategy.market.interval,
                start=end - step * (sizing.stop_atr_period + 5),
            )
        try:
            value = atr(candles, sizing.stop_atr_period)
        except ValueError as exc:
            raise ExecutionError(
                f"недостаточно свечей для ATR — укажите цену стопа явно ({exc})"
            ) from None
        return max(value * sizing.stop_atr_multiplier, mid * sizing.min_stop_distance_fraction)

    def _stop_trigger(self, position_side: Side, reference: Decimal, distance: Decimal) -> Decimal:
        # Округление к цене входа: защита никогда не ослабляется.
        if position_side is Side.BUY:
            return self.rules.round_price(reference - distance, Rounding.UP)
        return self.rules.round_price(reference + distance, Rounding.DOWN)

    # --- вход ---

    async def open_position(
        self,
        side: Side,
        notional: Decimal,
        *,
        confirm: Confirm | None,
        stop_price: Decimal | None = None,
    ) -> TradeResult:
        if self.strategy.execution.require_manual_confirmation and confirm is None:
            raise ExecutionError("требуется ручное подтверждение каждого ордера")
        snap = await self._snapshot()
        mid = snap.book.mid
        price = self._ioc_price(side, mid)
        size = self.rules.round_size_down(notional / mid)
        distance = (
            abs(mid - stop_price) if stop_price is not None else await self.stop_distance(mid)
        )
        stop_trigger = stop_price or self._stop_trigger(side, mid, distance)
        proposed = ProposedOrder(
            OrderPurpose.ENTRY, OrderKind.LIMIT_IOC, side, size, price, reduce_only=False
        )
        target_leverage = self.strategy.market.leverage
        order = await self._create(
            proposed,
            intent={
                "requested_notional": str(notional),
                "mid": str(mid),
                "raw_size": str(notional / mid),
                "raw_limit_price": str(
                    mid
                    * (
                        1
                        + (1 if side is Side.BUY else -1)
                        * self.strategy.limits.max_slippage_bps
                        / BPS
                    )
                ),
                "stop_trigger_planned": str(stop_trigger),
                "stop_distance": str(distance),
            },
        )
        # Первичная проверка — с плечом, которое будет выставлено перед отправкой.
        decision = self._guard(proposed, snap, leverage=target_leverage)
        if not decision.approved:
            return TradeResult(await self._block(order, decision.reasons))

        preview = OrderPreview(
            purpose=OrderPurpose.ENTRY,
            side=side,
            size=size,
            limit_price=price,
            notional=self.rules.notional(size, price),
            mid_price=mid,
            spread_bps=snap.book.spread_bps,
            position_size=snap.position_size,
            leverage_target=target_leverage,
            available_to_trade=snap.asset.available_to_trade_buy,
            stop_trigger=stop_trigger,
            max_loss_at_stop=size * abs(mid - stop_trigger),
        )
        if confirm is not None and not await confirm(preview):
            return TradeResult(await self._block(order, ("оператор отклонил ордер",)))

        await self.ensure_leverage()
        # Повторная проверка на свежем состоянии с фактическим плечом.
        snap = await self._snapshot()
        decision = self._guard(proposed, snap)
        if not decision.approved:
            return TradeResult(await self._block(order, decision.reasons))

        entry = await self._send(order.id, proposed)
        result = TradeResult(entry)
        if entry.filled_size > 0:
            reference = entry.avg_price or mid
            trigger = stop_price or self._stop_trigger(side, reference, distance)
            result.stop, warnings = await self.protect(
                position_side=side, size=entry.filled_size, trigger=trigger, parent=entry.order_id
            )
            result.warnings.extend(warnings)
        return result

    # --- защита ---

    async def protect(
        self,
        *,
        position_side: Side,
        size: Decimal,
        trigger: Decimal,
        parent: uuid.UUID | None = None,
    ) -> tuple[OrderResult | None, list[str]]:
        """Ставит reduceOnly stop-market и проверяет его фактическое наличие на бирже."""
        side = Side.SELL if position_side is Side.BUY else Side.BUY
        slip = self.strategy.execution.stop_market_slippage_bps / BPS
        limit = (
            self.rules.round_price(trigger * (1 - slip), Rounding.DOWN)
            if side is Side.SELL
            else self.rules.round_price(trigger * (1 + slip), Rounding.UP)
        )
        proposed = ProposedOrder(
            OrderPurpose.STOP,
            OrderKind.STOP_MARKET,
            side,
            self.rules.round_size_down(size),
            limit,
            reduce_only=True,
            trigger_price=trigger,
        )
        order = await self._create(
            proposed, intent={"stop_slippage_bps": str(slip * BPS)}, parent=parent
        )
        snap = await self._snapshot()
        decision = self._guard(proposed, snap)
        if not decision.approved:
            blocked = await self._block(order, decision.reasons)
            return blocked, await self._protection_failed(
                "stop не прошёл проверку: " + "; ".join(decision.reasons)
            )
        stop = await self._send(order.id, proposed)
        if stop.status in (OrderStatus.UNKNOWN, OrderStatus.SIGNED):
            stop = await self.resolve(stop.order_id)
        if stop.status is not OrderStatus.RESTING or not await self._stop_on_exchange(stop.cloid):
            return stop, await self._protection_failed(
                f"stop не подтверждён биржей (статус {stop.status}, {stop.error or 'без ошибки'})"
            )
        return stop, []

    async def _stop_on_exchange(self, cloid: str) -> bool:
        orders = await self.gateway.get_open_orders(self.user)
        return any(o.cloid == cloid and o.is_trigger and o.reduce_only for o in orders)

    async def _protection_failed(self, reason: str) -> list[str]:
        message = f"ПОЗИЦИЯ БЕЗ ЗАЩИТЫ: {reason}"
        logfire.error("execution: {message}", message=message)
        async with self.sm.begin() as db:
            await control.block_entries(db, self.network, actor=ENGINE_ACTOR, reason=message)
        return [message + " — новые входы заблокированы, закройте позицию или поставьте stop"]

    # --- закрытие ---

    async def close_position(self, *, confirm: Confirm | None) -> TradeResult:
        if self.strategy.execution.require_manual_confirmation and confirm is None:
            raise ExecutionError("требуется ручное подтверждение каждого ордера")
        snap = await self._snapshot()
        position = snap.position_size
        if position == 0:
            return TradeResult(None, warnings=["позиции нет"])
        side = Side.SELL if position > 0 else Side.BUY
        mid = snap.book.mid
        proposed = ProposedOrder(
            OrderPurpose.EXIT,
            OrderKind.LIMIT_IOC,
            side,
            abs(position),
            self._ioc_price(side, mid),
            reduce_only=True,
        )
        order = await self._create(proposed, intent={"position": str(position), "mid": str(mid)})
        decision = self._guard(proposed, snap)
        if not decision.approved:
            return TradeResult(await self._block(order, decision.reasons))
        preview = OrderPreview(
            purpose=OrderPurpose.EXIT,
            side=side,
            size=abs(position),
            limit_price=proposed.price,
            notional=self.rules.notional(position, proposed.price),
            mid_price=mid,
            spread_bps=snap.book.spread_bps,
            position_size=position,
            leverage_target=snap.asset.leverage,
            available_to_trade=snap.asset.available_to_trade_buy,
        )
        if confirm is not None and not await confirm(preview):
            return TradeResult(await self._block(order, ("оператор отклонил ордер",)))

        exit_result = await self._send(order.id, proposed)
        result = TradeResult(exit_result)
        remaining = (await self.gateway.get_account_state(self.user)).position(self.coin)
        stops = await self._active_stops()
        if remaining is None or remaining.size == 0:
            for stop in stops:
                await self.cancel(stop.id)
        elif stops and exit_result.filled_size > 0:
            # Частичное закрытие: защита пересоздаётся на оставшийся размер (§12.4).
            trigger = stops[0].trigger_price
            for stop in stops:
                await self.cancel(stop.id)
            if trigger is not None:
                result.stop, warnings = await self.protect(
                    position_side=Side.BUY if remaining.size > 0 else Side.SELL,
                    size=abs(remaining.size),
                    trigger=trigger,
                )
                result.warnings.extend(warnings)
        return result

    async def _active_stops(self) -> list[OrderRecord]:
        async with self.sm() as db:
            orders = await repo.list_orders(
                db,
                statuses=[OrderStatus.RESTING, OrderStatus.UNKNOWN],
                market_id=self.market_id,
            )
        return [o for o in orders if o.purpose == OrderPurpose.STOP.value]

    # --- отмена ---

    async def cancel(self, order_id: uuid.UUID) -> OrderResult:
        async with self.sm() as db:
            order = await repo.get_order(db, order_id)
        if order is None:
            raise ExecutionError(f"ордер {order_id} не найден")
        if is_final(OrderStatus(order.status), OrderKind(order.order_kind)):
            return OrderResult.of(order)
        response = await self.session.submit(
            actions.cancel_by_cloid_action(self.rules.asset_id, order.cloid)
        )
        logfire.info(
            "execution: отмена {cloid}: {response}", cloid=order.cloid, response=response.raw
        )
        return await self.resolve(order_id)

    # --- отправка ---

    async def _create(
        self, proposed: ProposedOrder, *, intent: dict[str, Any], parent: uuid.UUID | None = None
    ) -> OrderRecord:
        async with self.sm.begin() as db:
            return await repo.create_order(
                db,
                cloid=actions.new_cloid(),
                market_id=self.market_id,
                coin=self.coin,
                purpose=proposed.purpose.value,
                order_kind=proposed.kind.value,
                side=proposed.side.value,
                price=proposed.price,
                size=proposed.size,
                trigger_price=proposed.trigger_price,
                reduce_only=proposed.reduce_only,
                vault_address=self.session.wallet.vault_address,
                parent_order_id=parent,
                correlation_id=current_correlation_id(),
                intent=intent,
            )

    async def _block(self, order: OrderRecord, reasons: tuple[str, ...]) -> OrderResult:
        async with self.sm.begin() as db:
            locked = await repo.lock_order(db, order.id)
            await repo.transition(
                db,
                locked,
                OrderStatus.BLOCKED,
                source="guard",
                details={"reasons": list(reasons)},
                error="; ".join(reasons),
            )
            return OrderResult.of(locked, reasons)

    async def _send(self, order_id: uuid.UUID, proposed: ProposedOrder) -> OrderResult:
        async with self.sm.begin() as db:
            order = await repo.lock_order(db, order_id)
            await repo.transition(db, order, OrderStatus.APPROVED, source="guard")
            request = actions.OrderRequest(
                asset=self.rules.asset_id,
                is_buy=proposed.side is Side.BUY,
                price=proposed.price,
                size=proposed.size,
                reduce_only=proposed.reduce_only,
                order_type=(
                    actions.LimitOrderType(actions.Tif.IOC)
                    if proposed.kind is OrderKind.LIMIT_IOC
                    else actions.TriggerOrderType(
                        trigger_price=proposed.trigger_price or proposed.price,
                        is_market=True,
                        tpsl="sl",
                    )
                ),
                cloid=order.cloid,
            )
            action = actions.order_action([request])
        payload = await self.session.prepare(action)
        async with self.sm.begin() as db:
            order = await repo.lock_order(db, order_id)
            await repo.transition(
                db,
                order,
                OrderStatus.SIGNED,
                source="local",
                details={"nonce": payload["nonce"]},
                nonce=payload["nonce"],
                request_action=action,
                signed_at=self.clock(),
            )

        try:
            response = await self.session.send(payload)
        except ExchangeNotSentError as exc:
            return await self._finish(order_id, OrderStatus.NOT_SENT, error=str(exc))
        except HyperliquidRateLimitError as exc:
            return await self._finish(order_id, OrderStatus.REJECTED, error=str(exc))
        except ExchangeOutcomeUnknownError as exc:
            await self._finish(order_id, OrderStatus.UNKNOWN, error=str(exc))
            if isinstance(exc, HyperliquidFormatError):
                await self._integration_error(str(exc))
            return await self.resolve(order_id)
        except HyperliquidRequestError as exc:
            return await self._finish(order_id, OrderStatus.REJECTED, error=str(exc))
        return await self._apply_response(order_id, proposed, response)

    async def _apply_response(
        self, order_id: uuid.UUID, proposed: ProposedOrder, response: ExchangeResponse
    ) -> OrderResult:
        if not response.ok:
            return await self._finish(
                order_id, OrderStatus.REJECTED, error=response.error, response=response.raw
            )
        statuses = (
            (response.data or {}).get("statuses") if isinstance(response.data, dict) else None
        )
        try:
            if (
                response.response_type != "order"
                or not isinstance(statuses, list)
                or len(statuses) != 1
            ):
                raise PlacementFormatError(f"неожиданный ответ на ордер: {response.raw}")
            outcome = parse_placement_status(statuses[0])
        except PlacementFormatError as exc:
            await self._finish(order_id, OrderStatus.UNKNOWN, error=str(exc), response=response.raw)
            await self._integration_error(str(exc))
            return await self.resolve(order_id)

        if outcome.kind is PlacementKind.RESTING:
            return await self._finish(
                order_id, OrderStatus.RESTING, oid=outcome.oid, response=response.raw
            )
        if outcome.kind is PlacementKind.FILLED:
            status = (
                OrderStatus.FILLED
                if outcome.filled_size >= proposed.size
                else OrderStatus.PARTIALLY_FILLED
            )
            return await self._finish(
                order_id,
                status,
                oid=outcome.oid,
                filled_size=min(outcome.filled_size, proposed.size),
                avg_fill_price=outcome.avg_price,
                response=response.raw,
            )
        if outcome.kind is PlacementKind.ERROR:
            return await self._finish(
                order_id,
                OrderStatus.REJECTED,
                error=outcome.error,
                error_code=outcome.error_code.value if outcome.error_code else None,
                response=response.raw,
            )
        # Принят без oid (строковый статус) — уточняем по cloid.
        await self._finish(order_id, OrderStatus.UNKNOWN, response=response.raw)
        return await self.resolve(order_id)

    async def _finish(
        self,
        order_id: uuid.UUID,
        status: OrderStatus,
        *,
        source: str = "exchange_response",
        **updates: Any,
    ) -> OrderResult:
        async with self.sm.begin() as db:
            order = await repo.lock_order(db, order_id)
            await repo.transition(
                db,
                order,
                status,
                source=source,
                details={k: str(v) for k, v in updates.items() if k != "response"},
                **updates,
            )
            result = OrderResult.of(order)
        logfire.info(
            "execution: ордер {cloid} → {status}",
            cloid=result.cloid,
            status=result.status.value,
            filled=str(result.filled_size),
            error=result.error,
        )
        return result

    async def _integration_error(self, reason: str) -> None:
        """Непонятный ответ биржи: новые действия останавливаются до ручного разбора."""
        async with self.sm.begin() as db:
            await control.block_entries(
                db, self.network, actor=ENGINE_ACTOR, reason=f"ошибка интеграции: {reason}"[:500]
            )

    # --- сверка ---

    async def resolve(self, order_id: uuid.UUID) -> OrderResult:
        """Уточняет состояние ордера на бирже по cloid (§10.4): orderStatus → fills → таймаут."""
        async with self.sm() as db:
            order = await repo.get_order(db, order_id)
        if order is None:
            raise ExecutionError(f"ордер {order_id} не найден")
        current = OrderStatus(order.status)
        kind = OrderKind(order.order_kind)
        if is_final(current, kind) or current in (OrderStatus.CREATED, OrderStatus.APPROVED):
            return OrderResult.of(order)

        status = await self.gateway.get_order_status(self.user, order.cloid)
        if status.found and status.order is not None and status.status is not None:
            remote = status.order
            target = map_exchange_order_status(status.status, remote.original_size, remote.size)
            filled = remote.original_size - remote.size
            if target is OrderStatus.CANCELED and filled > 0 and kind is OrderKind.LIMIT_IOC:
                target = OrderStatus.PARTIALLY_FILLED
            if target is OrderStatus.UNKNOWN or (target == current and filled == order.filled_size):
                return OrderResult.of(order)
            avg = await self._avg_from_fills(order, since=order.created_at)
            return await self._finish(
                order_id,
                target,
                source="order_status",
                oid=remote.oid,
                filled_size=min(filled, order.size),
                avg_fill_price=avg or order.avg_fill_price,
            )

        fills = [
            f
            for f in await self.gateway.get_fills(
                self.user, start=order.created_at - timedelta(minutes=1)
            )
            if f.cloid == order.cloid or (order.oid is not None and f.oid == order.oid)
        ]
        if fills:
            async with self.sm.begin() as db:
                await repo.add_fills(db, self.market_id, fills)
            filled = min(sum((f.size for f in fills), Decimal(0)), order.size)
            avg = sum((f.size * f.price for f in fills), Decimal(0)) / sum(f.size for f in fills)
            target = OrderStatus.FILLED if filled >= order.size else OrderStatus.PARTIALLY_FILLED
            return await self._finish(
                order_id,
                target,
                source="fills",
                oid=fills[0].oid,
                filled_size=filled,
                avg_fill_price=avg,
            )

        signed_at = order.signed_at or order.created_at
        timeout = timedelta(seconds=self.strategy.freshness.max_unknown_order_seconds)
        if (
            current in (OrderStatus.SIGNED, OrderStatus.UNKNOWN)
            and self.clock() - signed_at > timeout
        ):
            return await self._finish(
                order_id,
                OrderStatus.REJECTED,
                source="order_status",
                error="не найден на бирже по cloid и нет исполнений: ордер не был принят",
            )
        return OrderResult.of(order)

    async def _avg_from_fills(self, order: OrderRecord, *, since: datetime) -> Decimal | None:
        fills = [
            f
            for f in await self.gateway.get_fills(self.user, start=since - timedelta(minutes=1))
            if f.cloid == order.cloid or (order.oid is not None and f.oid == order.oid)
        ]
        if not fills:
            return None
        async with self.sm.begin() as db:
            await repo.add_fills(db, self.market_id, fills)
        total = sum((f.size for f in fills), Decimal(0))
        return sum((f.size * f.price for f in fills), Decimal(0)) / total

    async def resolve_unresolved(self) -> list[OrderResult]:
        async with self.sm() as db:
            orders = await repo.list_orders(
                db, statuses=list(repo.UNRESOLVED), market_id=self.market_id, limit=200
            )
        return [await self.resolve(o.id) for o in orders]

    async def sync_fills(self, since: datetime) -> int:
        fills = await self.gateway.get_fills(self.user, start=since)
        fills = [f for f in fills if f.coin == self.coin]
        async with self.sm.begin() as db:
            return await repo.add_fills(db, self.market_id, fills)

    async def snapshot(self) -> None:
        state = await self.gateway.get_account_state(self.user)
        asset = await self.gateway.get_active_asset_data(self.user, self.coin)
        async with self.sm.begin() as db:
            await repo.add_account_snapshot(db, state, asset)
            await repo.add_position_snapshot(
                db, self.market_id, state.position(self.coin), state.exchange_time
            )

    # --- плечо ---

    async def ensure_leverage(self) -> None:
        """Isolated и плечо из spec; после установки проверяется фактическое значение (§12.2)."""
        target = self.strategy.market.leverage
        asset = await self.gateway.get_active_asset_data(self.user, self.coin)
        if asset.leverage_type == "isolated" and asset.leverage == target:
            return
        response = await self.session.submit(
            actions.update_leverage_action(self.rules.asset_id, is_cross=False, leverage=target)
        )
        if not response.ok:
            raise ExecutionError(f"биржа не установила плечо: {response.error}")
        after = await self.gateway.get_active_asset_data(self.user, self.coin)
        if after.leverage_type != "isolated" or after.leverage != target:
            raise ExecutionError(
                f"плечо после установки {after.leverage_type} {after.leverage}x, ожидалось "
                f"isolated {target}x"
            )
        async with self.sm.begin() as db:
            await control.record_audit(
                db,
                actor=ENGINE_ACTOR,
                action="leverage.updated",
                details={
                    "coin": self.coin,
                    "from": f"{asset.leverage_type} {asset.leverage}x",
                    "to": f"isolated {target}x",
                },
            )
