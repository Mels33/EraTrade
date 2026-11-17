"""Загрузка торговой и риск-спецификации из config/strategy.toml (см. docs/spec.md)."""

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUPPORTED_SPEC_VERSION = 1


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MarketConfig(_Frozen):
    coin: str = Field(min_length=1)
    interval: Literal["1h"]
    horizon_candles: int = Field(gt=0)
    margin_mode: Literal["isolated"]
    leverage: int = Field(ge=1)


class PolicyConfig(_Frozen):
    allow_long: bool
    allow_short: bool
    allow_reversal: Literal[False]
    time_exit_candles: int = Field(gt=0)
    reentry_cooldown_candles: int = Field(ge=1)


class SizingConfig(_Frozen):
    fixed_risk_usdc: Decimal = Field(gt=0)
    risk_fraction: Decimal = Field(gt=0, le=Decimal("0.05"))
    stop_atr_period: int = Field(gt=1)
    stop_atr_multiplier: Decimal = Field(gt=0)
    min_stop_distance_fraction: Decimal = Field(gt=0, lt=1)
    min_order_notional_usdc: Decimal = Field(gt=0)


class LimitsConfig(_Frozen):
    max_position_notional_usdc: Decimal = Field(gt=0)
    max_leverage: int = Field(ge=1)
    max_daily_loss_fraction: Decimal = Field(gt=0, lt=1)
    max_weekly_loss_fraction: Decimal = Field(gt=0, lt=1)
    max_trades_per_day: int = Field(gt=0)
    max_spread_bps: Decimal = Field(gt=0)
    max_slippage_bps: Decimal = Field(gt=0)


class FreshnessConfig(_Frozen):
    max_decision_delay_seconds: int = Field(gt=0)
    max_ws_silence_seconds: int = Field(gt=0)
    max_unknown_order_seconds: int = Field(gt=0)


class ExecutionConfig(_Frozen):
    stop_market_slippage_bps: Decimal = Field(gt=0, le=Decimal("2000"))
    require_manual_confirmation: bool


class RiskConfig(_Frozen):
    loss_includes_unrealized: bool
    address_requests_warning: int = Field(ge=0)


class DeadMansSwitchConfig(_Frozen):
    enabled: bool
    horizon_seconds: int = Field(ge=10)
    refresh_seconds: int = Field(ge=5)

    @model_validator(mode="after")
    def _refresh_before_horizon(self) -> "DeadMansSwitchConfig":
        if self.refresh_seconds >= self.horizon_seconds - 5:
            raise ValueError("refresh_seconds должен быть меньше horizon_seconds - 5")
        return self


class TargetConfig(_Frozen):
    """Разметка обучающего набора (§14.2): порог класса = издержки сделки + запас + край."""

    taker_fee_bps: Decimal = Field(ge=0)  # за одну сторону
    expected_spread_bps: Decimal = Field(ge=0)  # полный спред, платится один раз за сделку
    expected_slippage_bps: Decimal = Field(ge=0)  # за одну сторону
    expected_funding_bps: Decimal = Field(ge=0)  # за горизонт
    safety_margin_bps: Decimal = Field(ge=0)
    min_edge_bps: Decimal = Field(gt=0)

    @property
    def cost_bps(self) -> Decimal:
        """Ожидаемые издержки сделки туда-обратно."""
        return (
            2 * self.taker_fee_bps
            + self.expected_spread_bps
            + 2 * self.expected_slippage_bps
            + self.expected_funding_bps
        )

    @property
    def threshold_bps(self) -> Decimal:
        return self.cost_bps + self.safety_margin_bps + self.min_edge_bps


class StrategyConfig(_Frozen):
    spec_version: int
    market: MarketConfig
    policy: PolicyConfig
    sizing: SizingConfig
    limits: LimitsConfig
    freshness: FreshnessConfig
    execution: ExecutionConfig
    risk: RiskConfig
    dead_mans_switch: DeadMansSwitchConfig
    target: TargetConfig

    @model_validator(mode="after")
    def _check_cross_fields(self) -> "StrategyConfig":
        if self.spec_version != SUPPORTED_SPEC_VERSION:
            raise ValueError(
                f"spec_version={self.spec_version}, поддерживается {SUPPORTED_SPEC_VERSION}"
            )
        if self.market.leverage > self.limits.max_leverage:
            raise ValueError("market.leverage превышает limits.max_leverage")
        if self.limits.max_weekly_loss_fraction < self.limits.max_daily_loss_fraction:
            raise ValueError("недельный лимит убытка меньше дневного")
        if self.sizing.min_order_notional_usdc > self.limits.max_position_notional_usdc:
            raise ValueError("минимальный номинал ордера больше максимального номинала позиции")
        if not (self.policy.allow_long or self.policy.allow_short):
            raise ValueError("запрещены и long, и short")
        return self


def load_strategy(path: Path) -> StrategyConfig:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return StrategyConfig.model_validate(data)
