"""Минимальный guard исполнения (§10.0 плана) — действует с первого дня, до полного risk engine.

Проверяется на свежем состоянии биржи непосредственно перед подписью:
- режим работы разрешает это действие в этой сети;
- запрет входов (entries_blocked) соблюдён;
- не больше одной позиции, без усреднения/пирамидинга и без разворота;
- закрытие и защита только reduceOnly и не больше текущей позиции;
- размер и цена соответствуют правилам точности, номинал в пределах [минимум биржи, лимит];
- хватает доступной маржи при заданном плече;
- спред не шире лимита (для входа).
"""

from dataclasses import dataclass, field
from decimal import Decimal

from app.config.settings import Network, TradingMode
from app.config.strategy import StrategyConfig
from app.domain.account import Side
from app.domain.instruments import InstrumentRules
from app.domain.orders import OrderKind, OrderPurpose

ENTRY_MODES: dict[Network, frozenset[TradingMode]] = {
    Network.TESTNET: frozenset({TradingMode.TESTNET}),
    Network.MAINNET: frozenset({TradingMode.LIVE_ENABLED}),
}
PROTECTIVE_MODES: dict[Network, frozenset[TradingMode]] = {
    Network.TESTNET: frozenset({TradingMode.TESTNET}),
    Network.MAINNET: frozenset({TradingMode.LIVE_ENABLED, TradingMode.LIVE_PAUSED}),
}


@dataclass(frozen=True, slots=True)
class ProposedOrder:
    purpose: OrderPurpose
    kind: OrderKind
    side: Side
    size: Decimal
    price: Decimal  # лимитная цена (для stop-market — худшая цена исполнения)
    reduce_only: bool
    trigger_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class GuardContext:
    network: Network
    mode: TradingMode
    entries_blocked: bool
    block_reason: str | None
    position_size: Decimal  # со знаком: > 0 long, < 0 short, 0 — позиции нет
    open_entry_orders: int  # наши незавершённые входные ордера
    mark_price: Decimal
    available_to_trade: Decimal
    leverage: int
    spread_bps: Decimal | None


@dataclass(frozen=True, slots=True)
class GuardDecision:
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def approved(self) -> bool:
        return not self.reasons


def check_order(
    order: ProposedOrder,
    ctx: GuardContext,
    rules: InstrumentRules,
    strategy: StrategyConfig,
) -> GuardDecision:
    reasons: list[str] = []
    limits = strategy.limits

    # Режим и запрет входов.
    if order.purpose is OrderPurpose.ENTRY:
        if ctx.mode not in ENTRY_MODES[ctx.network]:
            reasons.append(f"режим {ctx.mode} не разрешает новые входы")
        if ctx.entries_blocked:
            reasons.append(f"новые входы запрещены: {ctx.block_reason}")
    elif ctx.mode not in PROTECTIVE_MODES[ctx.network]:
        reasons.append(f"режим {ctx.mode} не разрешает торговые действия")

    # Точность.
    if not rules.is_valid_size(order.size):
        reasons.append(f"размер {order.size} не соответствует szDecimals={rules.sz_decimals}")
    if not rules.is_valid_price(order.price):
        reasons.append(f"цена {order.price} не соответствует правилам точности")
    if order.trigger_price is not None and not rules.is_valid_price(order.trigger_price):
        reasons.append(f"цена срабатывания {order.trigger_price} не соответствует точности")

    position = ctx.position_size
    is_buy = order.side is Side.BUY

    if order.purpose is OrderPurpose.ENTRY:
        if order.reduce_only:
            reasons.append("входной ордер не может быть reduceOnly")
        if order.kind is not OrderKind.LIMIT_IOC:
            reasons.append("вход только IOC-ордером")
        if position != 0:
            reasons.append("позиция уже открыта: усреднение и разворот запрещены")
        if ctx.open_entry_orders:
            reasons.append("есть незавершённый входной ордер")
        if is_buy and not strategy.policy.allow_long:
            reasons.append("long запрещён политикой")
        if not is_buy and not strategy.policy.allow_short:
            reasons.append("short запрещён политикой")
        notional = rules.notional(order.size, order.price)
        if notional > limits.max_position_notional_usdc:
            reasons.append(
                f"номинал {notional:.2f} USDC больше лимита {limits.max_position_notional_usdc}"
            )
        expected_notional = rules.notional(order.size, ctx.mark_price)
        if expected_notional < strategy.sizing.min_order_notional_usdc:
            reasons.append(
                f"номинал {expected_notional:.2f} USDC меньше минимума "
                f"{strategy.sizing.min_order_notional_usdc}"
            )
        if ctx.leverage > limits.max_leverage:
            reasons.append(f"плечо {ctx.leverage}x больше лимита {limits.max_leverage}x")
        required_margin = notional / max(ctx.leverage, 1)
        if required_margin > ctx.available_to_trade:
            reasons.append(
                f"нужно маржи {required_margin:.2f} USDC, доступно {ctx.available_to_trade:.2f}"
            )
        if ctx.spread_bps is None:
            reasons.append("нет данных о спреде")
        elif ctx.spread_bps > limits.max_spread_bps:
            reasons.append(f"спред {ctx.spread_bps:.2f} bps шире лимита {limits.max_spread_bps}")
    else:
        # Закрытие и защита: только уменьшение существующей позиции.
        if not order.reduce_only:
            reasons.append("закрывающий ордер должен быть reduceOnly")
        if position == 0:
            reasons.append("позиции нет — закрывать нечего")
        elif (position > 0) == is_buy:
            reasons.append("сторона ордера не уменьшает позицию (разворот запрещён)")
        if abs(position) and order.size > abs(position):
            reasons.append(f"размер {order.size} больше позиции {abs(position)}")
        if order.purpose is OrderPurpose.STOP:
            if order.kind is not OrderKind.STOP_MARKET or order.trigger_price is None:
                reasons.append("защита — только stop-market с ценой срабатывания")
            elif position > 0 and order.trigger_price >= ctx.mark_price:
                reasons.append("stop для long должен быть ниже текущей цены")
            elif position < 0 and order.trigger_price <= ctx.mark_price:
                reasons.append("stop для short должен быть выше текущей цены")

    return GuardDecision(tuple(reasons))
