"""Доменные типы аккаунта: позиции, ордера, исполнения, лимиты запросов."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True, slots=True)
class Position:
    coin: str
    size: Decimal  # со знаком: > 0 long, < 0 short
    entry_price: Decimal | None
    position_value: Decimal
    unrealized_pnl: Decimal
    liquidation_price: Decimal | None
    margin_used: Decimal
    leverage_type: str  # "cross" | "isolated"
    leverage: int
    max_leverage: int
    funding_since_open: Decimal
    raw: dict[str, Any] = field(compare=False, repr=False)

    @property
    def is_long(self) -> bool:
        return self.size > 0


@dataclass(frozen=True, slots=True)
class AccountState:
    account_value: Decimal
    total_notional: Decimal
    total_margin_used: Decimal
    maintenance_margin_used: Decimal
    withdrawable: Decimal
    positions: tuple[Position, ...]
    exchange_time: datetime
    raw: dict[str, Any] = field(compare=False, repr=False)

    def position(self, coin: str) -> Position | None:
        return next((p for p in self.positions if p.coin == coin), None)


@dataclass(frozen=True, slots=True)
class OpenOrder:
    coin: str
    side: Side
    limit_price: Decimal
    size: Decimal
    original_size: Decimal
    oid: int
    cloid: str | None
    created_at: datetime
    order_type: str  # "Limit", "Stop Market", ...
    tif: str | None
    reduce_only: bool
    is_trigger: bool
    trigger_price: Decimal | None
    trigger_condition: str
    is_position_tpsl: bool
    raw: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class Fill:
    coin: str
    side: Side
    price: Decimal
    size: Decimal
    time: datetime
    start_position: Decimal
    direction: str  # "Open Long", "Close Short", "Long > Short", ...
    closed_pnl: Decimal
    fee: Decimal
    fee_token: str
    oid: int
    tid: int  # уникальный ID исполнения — ключ дедупликации
    hash: str
    crossed: bool
    cloid: str | None
    raw: dict[str, Any] = field(compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class OrderStatusResult:
    found: bool
    status: str | None = None  # "open", "filled", "canceled", "triggered", "rejected", ...
    order: OpenOrder | None = None
    status_time: datetime | None = None


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    cumulative_volume: Decimal
    requests_used: int
    requests_cap: int
    requests_surplus: int

    @property
    def remaining(self) -> int:
        return self.requests_cap + self.requests_surplus - self.requests_used


@dataclass(frozen=True, slots=True)
class UserRole:
    """Роль адреса на Hyperliquid (info userRole)."""

    role: str  # "user", "agent", "subAccount", "vault", "missing"
    agent_owner: str | None = None  # для agent: аккаунт, который одобрил API-кошелёк
    master: str | None = None  # для subAccount: основной аккаунт


@dataclass(frozen=True, slots=True)
class AgentInfo:
    """Именованный API-кошелёк аккаунта (info extraAgents)."""

    address: str
    name: str
    valid_until: datetime | None


@dataclass(frozen=True, slots=True)
class ActiveAssetData:
    """Плечо и доступная для торговли маржа по инструменту (info activeAssetData).

    available_to_trade учитывает spot USDC (единый баланс), в отличие от withdrawable
    из clearinghouseState.
    """

    coin: str
    leverage_type: str
    leverage: int
    max_trade_size_buy: Decimal
    max_trade_size_sell: Decimal
    available_to_trade_buy: Decimal
    available_to_trade_sell: Decimal
    mark_price: Decimal


@dataclass(frozen=True, slots=True)
class BestBidAsk:
    bid: Decimal
    ask: Decimal
    time: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal:
        return (self.ask - self.bid) / self.mid * 10_000
