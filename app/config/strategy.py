"""Конфигурация стратегии из config/strategy.toml: маркет-мейкинг (секция [mm])."""

import tomllib
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

SUPPORTED_SPEC_VERSION = 2


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class DeadMansSwitchConfig(_Frozen):
    enabled: bool
    horizon_seconds: int = Field(ge=10)
    refresh_seconds: int = Field(ge=5)

    @model_validator(mode="after")
    def _refresh_before_horizon(self) -> "DeadMansSwitchConfig":
        if self.refresh_seconds >= self.horizon_seconds - 5:
            raise ValueError("refresh_seconds должен быть меньше horizon_seconds - 5")
        return self


class MmConfig(_Frozen):
    """Маркет-мейкинг (config/strategy.toml, секция [mm])."""

    coins: tuple[str, ...] = ()  # стартовые монеты (при auto_select — до первого пересмотра)
    auto_select: bool = False
    max_coins: int = Field(default=2, ge=1, le=5)
    rescan_minutes: int = Field(default=180, ge=10)
    select_min_spread_bps: Decimal = Field(default=Decimal(5), gt=0)
    select_max_spread_bps: Decimal = Field(default=Decimal(30), gt=0)
    select_min_volume_usd: Decimal = Field(default=Decimal(2_000_000), ge=0)
    select_hysteresis: float = Field(default=0.3, ge=0)
    select_min_top_depth_usd: Decimal = Field(default=Decimal(50), ge=0)
    select_scan_top: int = Field(default=30, ge=5, le=200)
    adverse_selection_path: str = "data/mm/adverse_selection.json"
    exclude: tuple[str, ...] = ()
    retire_flatten_minutes: int = Field(default=60, ge=1)
    order_usd: Decimal = Field(default=Decimal(12), ge=10)  # биржевой минимум 10 USDC
    max_inventory_usd: Decimal = Field(default=Decimal(25), gt=0)
    daily_loss_usd: Decimal = Field(default=Decimal(2), gt=0)
    leverage: int = Field(default=3, ge=1)
    min_spread_bps: Decimal = Field(default=Decimal(4), gt=0)
    improve_min_ticks: int = Field(default=3, ge=2)
    min_edge_bps: Decimal = Field(default=Decimal(3), ge=0)  # отступ заявки от справедливой цены
    inventory_skew_bps: Decimal = Field(default=Decimal(3), ge=0)  # сдвиг цены при полной позиции
    flow_window_seconds: float = Field(default=30, gt=0)
    flow_min_usd: Decimal = Field(default=Decimal(1000), ge=0)
    flow_pull_imbalance: float = Field(default=0.6, gt=0, le=1)
    requote_min_interval_seconds: float = Field(default=3, ge=0)
    requote_slack_bps: Decimal = Field(default=Decimal(1), ge=0)  # допуск без перестановки
    max_actions_per_minute: int = Field(default=12, ge=2)
    min_requests_remaining: int = Field(default=20_000, ge=0)
    stale_book_seconds: float = Field(default=10, gt=0)
    sync_seconds: float = Field(default=5, gt=0)
    dms_horizon_seconds: int = Field(default=120, ge=10)
    dms_refresh_seconds: int = Field(default=60, ge=5)

    @model_validator(mode="after")
    def _consistent(self) -> "MmConfig":
        if self.max_inventory_usd < self.order_usd:
            raise ValueError("mm.max_inventory_usd должен быть не меньше mm.order_usd")
        if self.dms_refresh_seconds >= self.dms_horizon_seconds:
            raise ValueError("mm.dms_refresh_seconds должен быть меньше dms_horizon_seconds")
        return self


class WickConfig(_Frozen):
    """Ловля проколов с усреднением (config/strategy.toml, секция [wick])."""

    scan_path: str = "data/wick/scan.json"  # отбор монет (eratrade wick-scan)
    coins: tuple[str, ...] = ()  # ручной список вместо отбора (пусто — из scan_path)
    max_coins: int = Field(default=4, ge=1, le=10)
    base_usd: Decimal = Field(default=Decimal(12), ge=10)  # первая покупка (минимум 10 USDC)
    size_mult: Decimal = Field(default=Decimal("1.2"), ge=1, le=2)
    levels: int = Field(default=3, ge=1, le=6)
    leverage: int = Field(default=3, ge=1)
    daily_loss_usd: Decimal = Field(default=Decimal(5), ge=0)  # 0 — дневной стоп отключён
    requote_frac: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)  # доля шага
    max_actions_per_minute: int = Field(default=15, ge=2)
    sync_seconds: float = Field(default=5, gt=0)
    stop_slippage: Decimal = Field(default=Decimal("0.05"), gt=0, lt=1)  # предел цены стопа


class StrategyConfig(_Frozen):
    spec_version: int
    mm: MmConfig = MmConfig()
    wick: WickConfig = WickConfig()

    @model_validator(mode="after")
    def _check_version(self) -> "StrategyConfig":
        if self.spec_version != SUPPORTED_SPEC_VERSION:
            raise ValueError(
                f"spec_version={self.spec_version}, поддерживается {SUPPORTED_SPEC_VERSION}"
            )
        return self


def load_strategy(path: Path) -> StrategyConfig:
    with path.open("rb") as f:
        data = tomllib.load(f)
    return StrategyConfig.model_validate(data)
