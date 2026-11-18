from dataclasses import dataclass
from decimal import Decimal

import pytest

from app.domain.orders import (
    InvalidTransitionError,
    OrderErrorCode,
    OrderKind,
    OrderStatus,
    PlacementFormatError,
    PlacementKind,
    check_transition,
    classify_error,
    is_final,
    map_exchange_order_status,
    parse_placement_status,
)

S = OrderStatus


@pytest.mark.parametrize(
    ("message", "code"),
    [  # строки из документации Error responses
        ("Price must be divisible by tick size.", OrderErrorCode.TICK),
        ("Order must have minimum value of $10.", OrderErrorCode.MIN_NOTIONAL),
        ("Insufficient margin to place order.", OrderErrorCode.MARGIN),
        ("Reduce only order would increase position.", OrderErrorCode.REDUCE_ONLY),
        (
            "Post only order would have immediately matched, bbo was 1.",
            OrderErrorCode.BAD_ALO_PRICE,
        ),
        (
            "Order could not immediately match against any resting orders. asset=0",
            OrderErrorCode.IOC_NO_MATCH,
        ),
        ("Invalid TP/SL price.", OrderErrorCode.BAD_TRIGGER),
        ("No liquidity available for market order.", OrderErrorCode.NO_LIQUIDITY),
        (
            "Order would increase open interest while open interest is capped",
            OrderErrorCode.OPEN_INTEREST,
        ),
        ("Order price too far from oracle", OrderErrorCode.ORACLE),
        (
            "Order would cause position to exceed margin tier limit at current leverage",
            OrderErrorCode.MAX_POSITION,
        ),
        ("Something new", OrderErrorCode.OTHER),
    ],
)
def test_error_classification(message: str, code: OrderErrorCode) -> None:
    assert classify_error(message) is code


def test_placement_statuses() -> None:
    resting = parse_placement_status({"resting": {"oid": 77738308}})
    assert resting.kind is PlacementKind.RESTING and resting.oid == 77738308
    filled = parse_placement_status({"filled": {"totalSz": "0.02", "avgPx": "1891.4", "oid": 7}})
    assert filled.kind is PlacementKind.FILLED
    assert filled.filled_size == Decimal("0.02") and filled.avg_price == Decimal("1891.4")
    error = parse_placement_status({"error": "Order must have minimum value of $10."})
    assert error.kind is PlacementKind.ERROR and error.error_code is OrderErrorCode.MIN_NOTIONAL
    accepted = parse_placement_status("waitingForTrigger")
    assert accepted.kind is PlacementKind.ACCEPTED and accepted.oid is None


@pytest.mark.parametrize(
    "status", [{"weird": 1}, {"resting": {}}, {"filled": {"totalSz": "x"}}, 42, None]
)
def test_unknown_placement_status_raises(status: object) -> None:
    with pytest.raises(PlacementFormatError):
        parse_placement_status(status)


def test_transitions() -> None:
    check_transition(S.CREATED, S.APPROVED)
    check_transition(S.SIGNED, S.UNKNOWN)
    check_transition(S.UNKNOWN, S.FILLED)
    check_transition(S.RESTING, S.RESTING)  # обновление деталей
    for current, target in [
        (S.CREATED, S.SIGNED),  # подпись без проверки guard
        (S.FILLED, S.CANCELED),  # финальный статус неизменен
        (S.BLOCKED, S.APPROVED),
        (S.NOT_SENT, S.SIGNED),  # повтор — только новым ордером
        (S.UNKNOWN, S.NOT_SENT),  # неизвестный исход нельзя «списать» как неотправленный
        (S.FILLED, S.FILLED),
    ]:
        with pytest.raises(InvalidTransitionError):
            check_transition(current, target)


def test_final_states() -> None:
    assert is_final(S.PARTIALLY_FILLED, OrderKind.LIMIT_IOC)
    assert not is_final(S.PARTIALLY_FILLED, OrderKind.STOP_MARKET)
    assert not is_final(S.UNKNOWN, OrderKind.LIMIT_IOC)


@pytest.mark.parametrize(
    ("text", "orig", "remaining", "expected"),
    [
        ("open", "1", "1", S.RESTING),
        ("open", "1", "0.4", S.PARTIALLY_FILLED),
        ("filled", "1", "0", S.FILLED),
        ("triggered", "1", "1", S.TRIGGERED),
        ("canceled", "1", "1", S.CANCELED),
        ("marginCanceled", "1", "1", S.CANCELED),
        ("reduceOnlyCanceled", "1", "1", S.CANCELED),
        ("scheduledCancel", "1", "1", S.CANCELED),
        ("rejected", "1", "1", S.REJECTED),
        ("iocCancelRejected", "1", "1", S.REJECTED),
        ("somethingNew", "1", "1", S.UNKNOWN),
    ],
)
def test_order_status_mapping(text: str, orig: str, remaining: str, expected: OrderStatus) -> None:
    assert map_exchange_order_status(text, Decimal(orig), Decimal(remaining)) is expected


@dataclass
class Bar:
    high: Decimal
    low: Decimal
    close: Decimal
