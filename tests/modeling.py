"""Небольшая обученная модель для тестов (синтетическое блуждание, быстрые параметры)."""

from datetime import UTC, datetime
from pathlib import Path

from app.config.strategy import load_strategy
from app.domain.market import Candle, FundingPoint
from app.features.dataset import TargetParams, build_dataset, chronological_split, write_dataset
from app.models.artifact import ModelArtifact
from app.models.training import train_model
from app.signals.policy import PolicyParams
from tests.factories import HOUR, T0, candle_series, funding_series

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = load_strategy(ROOT / "config" / "strategy.toml")
PARAMS = PolicyParams.from_strategy(STRATEGY, HOUR)
N_CANDLES = 1200


def history() -> tuple[list[Candle], list[FundingPoint]]:
    candles = candle_series(N_CANDLES, seed=3)
    funding = funding_series(T0 - 30 * HOUR, T0 + (N_CANDLES + 5) * HOUR, seed=5)
    return candles, funding


def train_small(root: Path, *, iterations: int = 60, minute: int = 0) -> ModelArtifact:
    candles, funding = history()
    target = TargetParams(4, STRATEGY.target.threshold_bps, STRATEGY.target.cost_bps)
    dataset = build_dataset(candles, funding, target)
    parts = chronological_split(dataset.rows, 4 * HOUR)
    ds_dir = root / f"dataset-{iterations}-{minute}"
    write_dataset(dataset, parts, ds_dir, market={"coin": "BTC"}, project_root=ROOT)
    return train_model(
        ds_dir,
        STRATEGY,
        PARAMS,
        root / "models",
        catboost_params={"iterations": iterations, "od_wait": 20},
        now=datetime(2026, 11, 17, 12, minute, tzinfo=UTC),
    )
