"""Конфигурация config/strategy.toml: секция [mm] и проверки значений."""

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config.strategy import StrategyConfig, load_strategy

CONFIG = Path(__file__).resolve().parents[1] / "config" / "strategy.toml"


def test_repository_config_is_minimal_capital_market_making() -> None:
    mm = load_strategy(CONFIG).mm
    assert mm.coins and len(mm.coins) <= 2
    assert mm.order_usd >= 10  # биржевой минимум
    assert mm.max_inventory_usd <= Decimal(25)
    assert mm.daily_loss_usd <= Decimal(2)
    assert mm.dms_refresh_seconds < mm.dms_horizon_seconds


def _data() -> dict[str, Any]:
    with CONFIG.open("rb") as f:
        return tomllib.load(f)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("order_usd", 5),  # меньше биржевого минимума
        ("max_inventory_usd", 8),  # меньше заявки
        ("dms_refresh_seconds", 500),  # не раньше горизонта
        ("flow_pull_imbalance", 1.5),
        ("unknown_field", 1),
    ],
)
def test_invalid_mm_config_rejected(field: str, value: object) -> None:
    data = _data()
    data["mm"][field] = value
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(data)


def test_unsupported_spec_version_rejected() -> None:
    data = _data()
    data["spec_version"] = 1
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(data)
    data = _data()
    data["market"] = {"coin": "BTC"}  # старые секции больше не принимаются
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(data)
