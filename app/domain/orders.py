"""Жизненный цикл ордера (§10.1 плана) и разбор ответов биржи на ордера.

CREATED → APPROVED → SIGNED → RESTING / PARTIALLY_FILLED / FILLED / CANCELED / REJECTED / UNKNOWN
        ↘ BLOCKED (guard или оператор)   ↘ NOT_SENT (соединение не установлено)

UNKNOWN обязателен: запрос мог дойти до биржи, а ответ потеряться. Из UNKNOWN выводит
только сверка по cloid (orderStatus, fills), повторная отправка запрещена.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any


class OrderStatus(StrEnum):
    CREATED = "CREATED"
    BLOCKED = "BLOCKED"  # отклонён guard или оператором, на биржу не отправлялся
    APPROVED = "APPROVED"
    SIGNED = "SIGNED"  # подписан и сохранён непосредственно перед отправкой
    NOT_SENT = "NOT_SENT"  # соединение не установлено — запрос точно не ушёл
    RESTING = "RESTING"  # стоит в книге / ждёт срабатывания (trigger)
    TRIGGERED = "TRIGGERED"  # trigger сработал, исполняется
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class OrderPurpose(StrEnum):
    ENTRY = "entry"
    EXIT = "exit"
    STOP = "stop"


class OrderKind(StrEnum):
    LIMIT_IOC = "limit_ioc"
    STOP_MARKET = "stop_market"


TERMINAL = frozenset(
    {
        OrderStatus.BLOCKED,
        OrderStatus.NOT_SENT,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    }
)

_EXCHANGE_STATES = frozenset(
    {
        OrderStatus.RESTING,
        OrderStatus.TRIGGERED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELED,
        OrderStatus.REJECTED,
    }
)

ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.APPROVED, OrderStatus.BLOCKED}),
    OrderStatus.APPROVED: frozenset({OrderStatus.SIGNED, OrderStatus.BLOCKED}),
    OrderStatus.SIGNED: _EXCHANGE_STATES | {OrderStatus.NOT_SENT, OrderStatus.UNKNOWN},
    OrderStatus.UNKNOWN: _EXCHANGE_STATES,
    OrderStatus.RESTING: frozenset(
        {
            OrderStatus.TRIGGERED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
        }
    ),
    OrderStatus.TRIGGERED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELED}
    ),
}


class InvalidTransitionError(ValueError):
    pass


def check_transition(current: OrderStatus, target: OrderStatus) -> None:
    if current == target and current not in TERMINAL:
        return  # обновление деталей без смены статуса
    if target not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(f"переход {current} → {target} запрещён")


def is_final(status: OrderStatus, kind: OrderKind) -> bool:
    """Финальное ли состояние. Частично исполненный IOC тоже финален: остаток отменён биржей."""
    return status in TERMINAL or (
        status is OrderStatus.PARTIALLY_FILLED and kind is OrderKind.LIMIT_IOC
    )


# --- ошибки биржи (Error responses) ---


class OrderErrorCode(StrEnum):
    TICK = "tick"
    MIN_NOTIONAL = "min_notional"
    MARGIN = "margin"
    REDUCE_ONLY = "reduce_only"
    IOC_NO_MATCH = "ioc_no_match"
    BAD_ALO_PRICE = "bad_alo_price"
    BAD_TRIGGER = "bad_trigger"
    NO_LIQUIDITY = "no_liquidity"
    OPEN_INTEREST = "open_interest"
    ORACLE = "oracle"
    MAX_POSITION = "max_position"
    OTHER = "other"


_ERROR_PATTERNS: tuple[tuple[str, OrderErrorCode], ...] = (
    ("divisible by tick size", OrderErrorCode.TICK),
    ("minimum value of", OrderErrorCode.MIN_NOTIONAL),
    ("insufficient margin", OrderErrorCode.MARGIN),
    ("reduce only order would increase", OrderErrorCode.REDUCE_ONLY),
    ("could not immediately match", OrderErrorCode.IOC_NO_MATCH),
    ("post only order would have immediately matched", OrderErrorCode.BAD_ALO_PRICE),
    ("invalid tp/sl price", OrderErrorCode.BAD_TRIGGER),
    ("no liquidity available", OrderErrorCode.NO_LIQUIDITY),
    ("open interest", OrderErrorCode.OPEN_INTEREST),
    ("too far from oracle", OrderErrorCode.ORACLE),
    ("margin tier limit", OrderErrorCode.MAX_POSITION),
)


def classify_error(message: str) -> OrderErrorCode:
    lowered = message.lower()
    return next(
        (code for pattern, code in _ERROR_PATTERNS if pattern in lowered), OrderErrorCode.OTHER
    )


# --- разбор статуса размещения ---


class PlacementKind(StrEnum):
    RESTING = "resting"
    FILLED = "filled"
    ERROR = "error"
    ACCEPTED = "accepted"  # принят без oid (например, строковый статус trigger-ордера)


@dataclass(frozen=True, slots=True)
class PlacementOutcome:
    kind: PlacementKind
    oid: int | None = None
    filled_size: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    error: str | None = None
    error_code: OrderErrorCode | None = None
    raw: Any = None


class PlacementFormatError(ValueError):
    """Непонятный статус размещения: исход неизвестен."""


def parse_placement_status(status: Any) -> PlacementOutcome:
    if isinstance(status, str):
        return PlacementOutcome(kind=PlacementKind.ACCEPTED, raw=status)
    if not isinstance(status, dict):
        raise PlacementFormatError(f"статус ордера неизвестного формата: {status!r}")
    try:
        if "resting" in status:
            return PlacementOutcome(
                kind=PlacementKind.RESTING, oid=int(status["resting"]["oid"]), raw=status
            )
        if "filled" in status:
            filled = status["filled"]
            return PlacementOutcome(
                kind=PlacementKind.FILLED,
                oid=int(filled["oid"]),
                filled_size=Decimal(str(filled["totalSz"])),
                avg_price=Decimal(str(filled["avgPx"])),
                raw=status,
            )
        if "error" in status:
            message = str(status["error"])
            return PlacementOutcome(
                kind=PlacementKind.ERROR,
                error=message,
                error_code=classify_error(message),
                raw=status,
            )
    except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
        raise PlacementFormatError(f"статус ордера неизвестного формата: {status!r}") from exc
    raise PlacementFormatError(f"статус ордера неизвестного формата: {status!r}")


def map_exchange_order_status(
    text: str, original_size: Decimal, remaining_size: Decimal
) -> OrderStatus:
    """Статус из info orderStatus → наш статус."""
    if text == "open":
        return (
            OrderStatus.RESTING if remaining_size == original_size else OrderStatus.PARTIALLY_FILLED
        )
    if text == "filled":
        return OrderStatus.FILLED
    if text == "triggered":
        return OrderStatus.TRIGGERED
    if text == "canceled" or text.endswith("Canceled") or text == "scheduledCancel":
        return OrderStatus.CANCELED
    if text == "rejected" or text.endswith("Rejected"):
        return OrderStatus.REJECTED
    return OrderStatus.UNKNOWN
