"""Построение действий /exchange в точном wire-формате Hyperliquid.

Подпись — keccak от msgpack(action), а msgpack сохраняет порядок ключей, поэтому словари
строятся в том же порядке, что и в официальном SDK (это проверяется тестами против SDK).
Цены и размеры передаются строками из Decimal (без float) — значения уже должны быть
приведены к правилам точности инструмента (app.domain.instruments).
"""

import re
import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from app.domain.instruments import to_wire

_CLOID_RE = re.compile(r"^0x[0-9a-f]{32}$")


def new_cloid() -> str:
    """Клиентский ID ордера: 128 бит, 0x + 32 hex."""
    return "0x" + uuid.uuid4().hex


def validate_cloid(cloid: str) -> str:
    if not _CLOID_RE.match(cloid):
        raise ValueError("cloid должен иметь вид 0x + 32 hex-символа в нижнем регистре")
    return cloid


class Tif(StrEnum):
    IOC = "Ioc"
    GTC = "Gtc"
    ALO = "Alo"


@dataclass(frozen=True, slots=True)
class LimitOrderType:
    tif: Tif

    def to_wire(self) -> dict[str, Any]:
        return {"limit": {"tif": self.tif.value}}


@dataclass(frozen=True, slots=True)
class TriggerOrderType:
    trigger_price: Decimal
    is_market: bool
    tpsl: Literal["tp", "sl"]

    def to_wire(self) -> dict[str, Any]:
        return {
            "trigger": {
                "isMarket": self.is_market,
                "triggerPx": to_wire(self.trigger_price),
                "tpsl": self.tpsl,
            }
        }


@dataclass(frozen=True, slots=True)
class OrderRequest:
    asset: int
    is_buy: bool
    price: Decimal
    size: Decimal
    reduce_only: bool
    order_type: LimitOrderType | TriggerOrderType
    cloid: str | None = None

    def __post_init__(self) -> None:
        if self.price <= 0 or self.size <= 0:
            raise ValueError("цена и размер ордера должны быть положительными")
        if self.cloid is not None:
            validate_cloid(self.cloid)

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "a": self.asset,
            "b": self.is_buy,
            "p": to_wire(self.price),
            "s": to_wire(self.size),
            "r": self.reduce_only,
            "t": self.order_type.to_wire(),
        }
        if self.cloid is not None:
            wire["c"] = self.cloid
        return wire


def order_action(orders: list[OrderRequest], grouping: str = "na") -> dict[str, Any]:
    if not orders:
        raise ValueError("пустой список ордеров")
    return {"type": "order", "orders": [o.to_wire() for o in orders], "grouping": grouping}


def cancel_action(asset: int, oid: int) -> dict[str, Any]:
    return {"type": "cancel", "cancels": [{"a": asset, "o": oid}]}


def cancel_by_cloid_action(asset: int, cloid: str) -> dict[str, Any]:
    return {"type": "cancelByCloid", "cancels": [{"asset": asset, "cloid": validate_cloid(cloid)}]}


def cancel_many_action(cancels: list[tuple[int, int]]) -> dict[str, Any]:
    """Пакетная отмена по oid: [(asset, oid), ...]."""
    if not cancels:
        raise ValueError("пустой список отмен")
    return {"type": "cancel", "cancels": [{"a": a, "o": o} for a, o in cancels]}


def cancel_by_cloid_many_action(cancels: list[tuple[int, str]]) -> dict[str, Any]:
    if not cancels:
        raise ValueError("пустой список отмен")
    return {
        "type": "cancelByCloid",
        "cancels": [{"asset": a, "cloid": validate_cloid(c)} for a, c in cancels],
    }


def batch_modify_action(modifies: list[tuple[int | str, OrderRequest]]) -> dict[str, Any]:
    """Перестановка ордеров: [(oid или cloid старого ордера, новый ордер), ...] (формат SDK)."""
    if not modifies:
        raise ValueError("пустой список перестановок")
    wires = []
    for target, order in modifies:
        if isinstance(target, str):
            validate_cloid(target)
        wires.append({"oid": target, "order": order.to_wire()})
    return {"type": "batchModify", "modifies": wires}


def update_leverage_action(asset: int, *, is_cross: bool, leverage: int) -> dict[str, Any]:
    if leverage < 1:
        raise ValueError("плечо должно быть >= 1")
    return {"type": "updateLeverage", "asset": asset, "isCross": is_cross, "leverage": leverage}


def schedule_cancel_action(time_ms: int | None) -> dict[str, Any]:
    """Dead man's switch: отмена всех ордеров в момент time_ms; None — снять расписание."""
    action: dict[str, Any] = {"type": "scheduleCancel"}
    if time_ms is not None:
        action["time"] = time_ms
    return action


def action_weight(action: dict[str, Any]) -> int:
    """Вес для IP-лимита: 1 + floor(batch_length / 40)."""
    batch = action.get("orders") or action.get("cancels") or action.get("modifies") or []
    return 1 + len(batch) // 40
