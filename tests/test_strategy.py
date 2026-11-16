import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.config.strategy import StrategyConfig, load_strategy

CONFIG = Path(__file__).resolve().parents[1] / "config" / "strategy.toml"


def raw() -> dict[str, Any]:
    with CONFIG.open("rb") as f:
        return tomllib.load(f)


def test_repository_config_matches_spec() -> None:
    s = load_strategy(CONFIG)
    assert s.market.coin == "BTC"
    assert s.market.interval == "1h"
    assert s.market.horizon_candles == 4
    assert s.policy.allow_reversal is False
    assert s.policy.time_exit_candles == 8
    assert s.sizing.fixed_risk_usdc == Decimal("10")
    assert s.sizing.risk_fraction == Decimal("0.005")
    assert s.limits.max_position_notional_usdc == Decimal("200")
    assert s.limits.max_daily_loss_fraction == Decimal("0.02")
    assert s.limits.max_trades_per_day == 4
    assert s.freshness.max_ws_silence_seconds == 30


@pytest.mark.parametrize(
    ("section", "key", "value"),
    [
        ("policy", "allow_reversal", True),
        ("market", "leverage", 5),  # больше max_leverage
        ("market", "margin_mode", "cross"),
        ("sizing", "risk_fraction", "0.5"),
        ("sizing", "fixed_risk_usdc", "-1"),
        ("limits", "max_weekly_loss_fraction", "0.01"),  # меньше дневного
        ("limits", "max_position_notional_usdc", "5"),  # меньше мин. номинала ордера
        ("limits", "unknown_limit", 1),
    ],
)
def test_invalid_config_rejected(section: str, key: str, value: object) -> None:
    data = raw()
    data[section][key] = value
    with pytest.raises(ValidationError):
        StrategyConfig.model_validate(data)


def test_unsupported_spec_version_rejected() -> None:
    data = raw()
    data["spec_version"] = 2
    with pytest.raises(ValidationError, match="spec_version"):
        StrategyConfig.model_validate(data)
