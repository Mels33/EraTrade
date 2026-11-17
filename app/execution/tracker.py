"""Отслеживание ордеров по данным биржи — без ключа и без торговой сессии.

Используется и execution engine (после отправки), и сверкой (отдельным процессом без
доступа к ключу). Источник истины — биржа: orderStatus по cloid, затем fills.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import logfire
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import OrderRecord
from app.db.repositories import trading as repo
from app.domain.orders import (
    InvalidTransitionError,
    OrderKind,
    OrderStatus,
    is_final,
    map_exchange_order_status,
)
from app.integrations.hyperliquid.gateway import HyperliquidGateway

ABANDONED_REASON = "прерван до подписи (процесс остановился): на биржу не отправлялся"


def _utc_now() -> datetime:
    return datetime.now(UTC)


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


class OrderNotFoundError(LookupError):
    pass


class OrderTracker:
    def __init__(
        self,
        gateway: HyperliquidGateway,
        sessionmaker: async_sessionmaker[AsyncSession],
        *,
        market_id: int,
        coin: str,
        user: str,
        max_unknown_seconds: int,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.gateway = gateway
        self.sm = sessionmaker
        self.market_id = market_id
        self.coin = coin
        self.user = user
        self.max_unknown = timedelta(seconds=max_unknown_seconds)
        self.clock = clock

    async def finish(
        self,
        order_id: uuid.UUID,
        status: OrderStatus,
        *,
        source: str = "exchange_response",
        extra_details: dict[str, Any] | None = None,
        **updates: Any,
    ) -> OrderResult:
        details = {k: str(v) for k, v in updates.items() if k != "response"}
        details.update(extra_details or {})
        async with self.sm.begin() as db:
            order = await repo.lock_order(db, order_id)
            try:
                await repo.transition(db, order, status, source=source, details=details, **updates)
            except InvalidTransitionError:
                # Ордер уже обновлён другим процессом (например, сверкой) — не перетираем.
                if not is_final(OrderStatus(order.status), OrderKind(order.order_kind)):
                    raise
                logfire.info(
                    "tracker: {cloid} уже в {current}, пропуск {target}",
                    cloid=order.cloid,
                    current=order.status,
                    target=status.value,
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

    async def resolve(self, order_id: uuid.UUID) -> OrderResult:
        """Уточняет состояние по cloid (§10.4): orderStatus → fills → таймаут неизвестности."""
        async with self.sm() as db:
            order = await repo.get_order(db, order_id)
        if order is None:
            raise OrderNotFoundError(f"ордер {order_id} не найден")
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
            fills = await self._fills_for(order)
            avg = _vwap(fills) if fills else order.avg_fill_price
            return await self.finish(
                order_id,
                target,
                source="order_status",
                extra_details={"exchange_status": status.status},
                oid=remote.oid,
                filled_size=min(filled, order.size),
                avg_fill_price=avg,
            )

        fills = await self._fills_for(order)
        if fills:
            filled = min(sum((f.size for f in fills), Decimal(0)), order.size)
            target = OrderStatus.FILLED if filled >= order.size else OrderStatus.PARTIALLY_FILLED
            return await self.finish(
                order_id,
                target,
                source="fills",
                oid=fills[0].oid,
                filled_size=filled,
                avg_fill_price=_vwap(fills),
            )

        signed_at = order.signed_at or order.created_at
        if (
            current in (OrderStatus.SIGNED, OrderStatus.UNKNOWN)
            and self.clock() - signed_at > self.max_unknown
        ):
            return await self.finish(
                order_id,
                OrderStatus.REJECTED,
                source="order_status",
                error="не найден на бирже по cloid и нет исполнений: ордер не был принят",
            )
        return OrderResult.of(order)

    async def _fills_for(self, order: OrderRecord) -> list[Any]:
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
        return fills

    async def resolve_unresolved(self) -> list[OrderResult]:
        async with self.sm() as db:
            orders = await repo.list_orders(
                db, statuses=list(repo.UNRESOLVED), market_id=self.market_id, limit=200
            )
        return [await self.resolve(o.id) for o in orders]

    async def abandon_stale_intents(self, older_than: timedelta) -> list[OrderResult]:
        """CREATED/APPROVED, не дошедшие до подписи (процесс остановился) → BLOCKED.

        Безопасно: без подписи и nonce запрос на биржу физически не мог уйти.
        """
        async with self.sm() as db:
            stale = await repo.list_stale_intents(db, self.market_id, older_than)
        results = []
        for order in stale:
            results.append(
                await self.finish(
                    order.id, OrderStatus.BLOCKED, source="reconcile", error=ABANDONED_REASON
                )
            )
        return results

    async def sync_fills(self, since: datetime) -> int:
        fills = [
            f for f in await self.gateway.get_fills(self.user, start=since) if f.coin == self.coin
        ]
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


def _vwap(fills: list[Any]) -> Decimal:
    total = sum((f.size for f in fills), Decimal(0))
    return sum((f.size * f.price for f in fills), Decimal(0)) / total
