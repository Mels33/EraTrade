"""Размер позиции от риска (§12.1, spec §4) — чистая функция."""

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.config.strategy import StrategyConfig, load_strategy
from app.domain.account import Side
from app.domain.instruments import InstrumentRules
from app.risk.sizing import size_position
from tests.factories import market_meta

STRATEGY = load_strategy(Path(__file__).resolve().parents[1] / "config" / "strategy.toml")
RULES = InstrumentRules.for_perp(market_meta(sz_decimals=5))


def with_limits(**limits: Any) -> StrategyConfig:
    return STRATEGY.model_copy(update={"limits": STRATEGY.limits.model_copy(update=limits)})


def sized(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "side": Side.BUY,
        "equity": Decimal("1000"),
        "entry_price": Decimal("65000"),
        "stop_price": Decimal("64000"),
        "available_margin": Decimal("10000"),
        "leverage": 2,
        "strategy": with_limits(max_position_notional_usdc=Decimal("100000")),
        "rules": RULES,
    }
    values.update(overrides)
    return size_position(**values)


def test_risk_amount_is_min_of_fixed_and_fraction() -> None:
    assert sized(equity=Decimal("1000")).risk_amount == Decimal("5")  # 0.5% < 10
    assert sized(equity=Decimal("100000")).risk_amount == Decimal("10")  # фикс. 10 < 500


def test_size_from_risk_and_stop_distance() -> None:
    result = sized()  # 5 USDC / 1000 = 0.005 BTC
    assert result.approved and result.size == Decimal("0.005")
    assert result.stop_distance == Decimal("1000")
    assert result.limited_by == ("риск 5.0000 USDC до стопа",)


def test_short_side_distance() -> None:
    result = sized(side=Side.SELL, stop_price=Decimal("66000"))
    assert result.approved and result.size == Decimal("0.005")


@pytest.mark.parametrize(
    ("overrides", "label"),
    [
        ({"strategy": STRATEGY}, "лимит номинала"),  # 20 USDC
        ({"available_margin": Decimal("100")}, "маржа"),  # 100 × 2 = 200 USDC
        ({"requested_notional": Decimal("50")}, "запрошено"),
    ],
)
def test_size_only_decreases_by_caps(overrides: dict[str, Any], label: str) -> None:
    result = sized(**overrides)
    assert result.approved and result.size < Decimal("0.005")
    assert any(label in item for item in result.limited_by)
    assert result.notional <= Decimal("200")


def test_size_rounded_down() -> None:
    result = sized(stop_price=Decimal("64001"))  # 5 / 999 = 0.0050050…
    assert result.size == Decimal("0.00500")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"stop_price": Decimal("66000")}, "неверной стороне"),
        ({"stop_price": Decimal("64900")}, "ближе минимальной дистанции"),  # 0.15% < 0.5%
        ({"equity": Decimal("0")}, "торговать нечем"),
        ({"equity": Decimal("10")}, "меньше минимума биржи"),  # риск 0.05 USDC → 3 USDC
        ({"strategy": with_limits(max_position_notional_usdc=Decimal("5"))}, "меньше минимума"),
    ],
)
def test_refusals(overrides: dict[str, Any], reason: str) -> None:
    result = sized(**overrides)
    assert not result.approved and result.size == 0
    assert result.refused is not None and reason in result.refused
