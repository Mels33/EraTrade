"""Target, метрики, обучающий набор и хронологическое разбиение."""

import dataclasses
import json
import math
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from app.config.strategy import load_strategy
from app.domain.market import Candle, FundingPoint
from app.features.dataset import (
    TargetParams,
    build_dataset,
    chronological_split,
    write_dataset,
)
from app.features.pipeline import LOOKBACK_CANDLES, features_for
from app.features.target import Label, forward_return, label_for
from app.models.metrics import brier, class_distribution, log_loss, prior_baseline
from tests.factories import HOUR, T0, candle_series, funding_series

ROOT = Path(__file__).resolve().parents[1]
TARGET = TargetParams(horizon_candles=4, threshold_bps=Decimal("39.5"), cost_bps=Decimal("14.5"))
N = LOOKBACK_CANDLES + 400


@pytest.fixture(scope="module")
def candles() -> list[Candle]:
    return candle_series(N)


@pytest.fixture(scope="module")
def funding() -> list[FundingPoint]:
    return funding_series(T0 - 30 * HOUR, T0 + (N + 10) * HOUR)


def test_threshold_from_strategy_config() -> None:
    target = load_strategy(ROOT / "config" / "strategy.toml").target
    # 2×4.5 комиссия + 1 спред + 2×2 проскальзывание + 0.5 funding = 14.5; +5 запас +20 край
    assert target.cost_bps == Decimal("14.5")
    assert target.threshold_bps == Decimal("39.5")


@pytest.mark.parametrize(
    ("ret", "label"),
    [(0.00396, Label.UP), (0.00395, Label.FLAT), (-0.00395, Label.FLAT), (-0.004, Label.DOWN)],
)
def test_label_boundaries(ret: float, label: Label) -> None:
    assert label_for(ret, Decimal("39.5")) is label


def test_forward_return() -> None:
    assert forward_return(Decimal("100"), Decimal("101")) == pytest.approx(0.01)
    with pytest.raises(ValueError):
        forward_return(Decimal("0"), Decimal("1"))


def test_metrics_known_values() -> None:
    labels = [Label.UP, Label.DOWN]
    perfect = [[0.0, 0.0, 1.0], [1.0, 0.0, 0.0]]
    assert log_loss(labels, perfect) == pytest.approx(0.0, abs=1e-12)
    assert brier(labels, perfect) == 0.0
    uniform = [[1 / 3] * 3] * 2
    assert log_loss(labels, uniform) == pytest.approx(math.log(3))
    assert brier(labels, uniform) == pytest.approx(2 / 3)
    assert class_distribution([Label.UP, Label.UP, Label.FLAT, Label.DOWN]) == {
        "DOWN": 0.25,
        "FLAT": 0.25,
        "UP": 0.5,
    }
    base = prior_baseline([Label.UP, Label.DOWN], [Label.UP])
    assert base.prior == {"DOWN": 0.5, "FLAT": 0.0, "UP": 0.5}
    assert base.log_loss == pytest.approx(math.log(2))


def test_rows_match_live_features_and_labels(
    candles: list[Candle], funding: list[FundingPoint]
) -> None:
    dataset = build_dataset(candles, funding, TARGET)
    assert len(dataset.rows) == N - LOOKBACK_CANDLES + 1 - TARGET.horizon_candles
    assert not dataset.skipped
    # Произвольная строка совпадает с вектором, посчитанным «как в реальном времени»
    # только по данным, известным к моменту решения.
    i = LOOKBACK_CANDLES + 123
    row = next(r for r in dataset.rows if r.decision_time == candles[i].close_time)
    known_funding = [f for f in funding if f.time < candles[i].close_time]
    _, live = features_for(candles[: i + 1], known_funding)
    assert row.features == live.values
    expected = forward_return(candles[i].close, candles[i + 4].close)
    assert row.fwd_return == expected and row.label is label_for(expected, TARGET.threshold_bps)


def test_gaps_are_skipped_not_bridged(candles: list[Candle], funding: list[FundingPoint]) -> None:
    gap_at = LOOKBACK_CANDLES + 200
    with_gap = candles[:gap_at] + candles[gap_at + 1 :]
    dataset = build_dataset(with_gap, funding, TARGET)
    assert dataset.skipped["пропуск в горизонте"] == TARGET.horizon_candles
    assert dataset.skipped["пропуск свечей"] == LOOKBACK_CANDLES - 1
    missing = candles[gap_at].open_time
    for row in dataset.rows:  # ни одно окно и ни один горизонт не перекрывают пропуск
        assert not (row.decision_time - LOOKBACK_CANDLES * HOUR <= missing < row.decision_time)
        assert not (row.decision_time <= missing < row.decision_time + 4 * HOUR)


def test_unclosed_candles_ignored(candles: list[Candle], funding: list[FundingPoint]) -> None:
    open_last = [*candles[:-1], dataclasses.replace(candles[-1], is_closed=False)]
    assert len(build_dataset(open_last, funding, TARGET).rows) == len(
        build_dataset(candles[:-1], funding, TARGET).rows
    )


def test_chronological_split_with_purge(candles: list[Candle], funding: list[FundingPoint]) -> None:
    dataset = build_dataset(candles, funding, TARGET)
    horizon = 4 * HOUR
    parts = chronological_split(dataset.rows, horizon)
    assert [p.name for p in parts] == ["train", "validation", "calibration", "test"]
    for prev, cur in pairwise(parts):
        assert prev.end is not None and cur.start is not None
        assert cur.start >= prev.end + horizon  # метки train не видят цены validation
    used = sum(len(p.rows) for p in parts)
    assert used == len(dataset.rows) - 3 * 3  # по 3 строки purge на каждой из 3 границ
    with pytest.raises(ValueError):
        chronological_split(dataset.rows, horizon, (0.5, 0.5, 0.1, 0.1))


def test_written_dataset_is_reproducible(
    tmp_path: Path, candles: list[Candle], funding: list[FundingPoint]
) -> None:
    manifests = []
    for name in ("a", "b"):
        dataset = build_dataset(candles, funding, TARGET)
        parts = chronological_split(dataset.rows, 4 * HOUR)
        csv_path, manifest = write_dataset(
            dataset, parts, tmp_path / name, market={"coin": "BTC"}, project_root=ROOT
        )
        manifests.append(manifest)
    assert manifests[0]["dataset_sha256"] == manifests[1]["dataset_sha256"]
    assert manifests[0]["source"]["sha256"] == manifests[1]["source"]["sha256"]
    assert (tmp_path / "a" / "dataset.csv").read_bytes() == (
        tmp_path / "b" / "dataset.csv"
    ).read_bytes()

    saved = json.loads((tmp_path / "a" / "manifest.json").read_text())
    assert saved["feature_set_version"] == "v1" and saved["target"]["threshold_bps"] == "39.5"
    assert set(saved["splits"]) == {"train", "validation", "calibration", "test"}
    assert "baseline_prior" in saved["splits"]["test"]

    header, first = csv_path.read_text().splitlines()[:2]
    columns = header.split(",")
    assert columns[:5] == ["decision_time", "split", "label", "fwd_return", "source"]
    assert len(first.split(",")) == len(columns)
    # repr(float) обратим: значения в CSV восстанавливаются бит в бит
    row = dataset.rows[0]
    assert first.split(",")[4] == "hyperliquid"
    assert tuple(float(x) for x in first.split(",")[5:]) == row.features
