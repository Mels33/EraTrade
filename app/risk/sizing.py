"""Размер позиции от допустимого риска (§12.1 плана, spec §4).

    risk_amount = min(fixed_risk_usdc, equity × risk_fraction)
    size        = risk_amount / |entry − stop|

Затем размер только уменьшается: лимит номинала, маржа × плечо, запрошенный номинал,
szDecimals (вниз). Если после этого номинал меньше биржевого минимума, стоп на неверной
стороне или ближе минимальной дистанции — сделка запрещается.
"""

from dataclasses import dataclass
from decimal import Decimal

from app.config.strategy import StrategyConfig
from app.domain.account import Side
from app.domain.instruments import InstrumentRules


@dataclass(frozen=True, slots=True)
class SizingResult:
    size: Decimal
    risk_amount: Decimal
    stop_distance: Decimal
    notional: Decimal
    limited_by: tuple[str, ...]
    refused: str | None = None

    @property
    def approved(self) -> bool:
        return self.refused is None and self.size > 0


def size_position(
    *,
    side: Side,
    equity: Decimal,
    entry_price: Decimal,
    stop_price: Decimal,
    available_margin: Decimal,
    leverage: int,
    strategy: StrategyConfig,
    rules: InstrumentRules,
    requested_notional: Decimal | None = None,
) -> SizingResult:
    sizing, limits = strategy.sizing, strategy.limits
    distance = entry_price - stop_price if side is Side.BUY else stop_price - entry_price
    risk_amount = min(sizing.fixed_risk_usdc, max(equity, Decimal(0)) * sizing.risk_fraction)

    def refuse(reason: str, limited: tuple[str, ...] = ()) -> SizingResult:
        return SizingResult(Decimal(0), risk_amount, distance, Decimal(0), limited, reason)

    if equity <= 0:
        return refuse(f"equity {equity} — торговать нечем")
    if distance <= 0:
        return refuse("стоп на неверной стороне от цены входа")
    if distance < entry_price * sizing.min_stop_distance_fraction:
        return refuse(
            f"стоп ближе минимальной дистанции {sizing.min_stop_distance_fraction * 100}% цены"
        )

    candidates: list[tuple[Decimal, str]] = [
        (risk_amount / distance, f"риск {risk_amount:.4f} USDC до стопа"),
        (
            limits.max_position_notional_usdc / entry_price,
            f"лимит номинала {limits.max_position_notional_usdc} USDC",
        ),
        (
            available_margin * min(leverage, limits.max_leverage) / entry_price,
            f"маржа: доступно {available_margin:.2f} USDC при плече {leverage}x",
        ),
    ]
    if requested_notional is not None:
        candidates.append(
            (requested_notional / entry_price, f"запрошено {requested_notional} USDC")
        )

    raw, _ = min(candidates, key=lambda c: c[0])
    limited = tuple(label for value, label in candidates if value == raw)
    size = rules.round_size_down(raw)
    notional = rules.notional(size, entry_price)
    if notional < sizing.min_order_notional_usdc or size <= 0:
        return refuse(
            f"размер после ограничений ({size}, ≈{notional:.2f} USDC) меньше минимума биржи "
            f"{sizing.min_order_notional_usdc} USDC; ограничение: {', '.join(limited)}",
            limited,
        )
    return SizingResult(size, risk_amount, distance, notional, limited)
