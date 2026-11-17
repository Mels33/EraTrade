"""Калибровка, обучение, артефакт и воспроизводимость прогноза конкретной версии."""

import dataclasses
import json
import math
import random
import shutil
from datetime import timedelta
from pathlib import Path

import pytest

from app.config.strategy import load_strategy
from app.features.dataset import TargetParams, build_dataset, chronological_split, write_dataset
from app.features.pipeline import FEATURE_NAMES, features_for
from app.features.target import Label
from app.models.artifact import ArtifactError, load_artifact
from app.models.calibration import fit_temperature, nll, softmax
from app.models.predictor import OutOfBoundsError, Predictor
from app.models.training import TrainingError, load_dataset_dir, train_model
from app.signals.policy import PolicyParams
from tests.factories import HOUR, T0, candle_series, funding_series

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = load_strategy(ROOT / "config" / "strategy.toml")
PARAMS = PolicyParams.from_strategy(STRATEGY, HOUR)
FAST = {"iterations": 60, "od_wait": 20}
N = 1200


def test_softmax_and_temperature() -> None:
    assert sum(softmax([1.0, 2.0, 3.0])) == pytest.approx(1.0)
    assert softmax([0.0, 0.0, 0.0]) == pytest.approx((1 / 3, 1 / 3, 1 / 3))
    # Переуверенные логиты (×3 к истинным): подобранная T ≈ 3 и log loss лучше, чем при T=1
    rng = random.Random(1)  # noqa: S311
    classes = [Label.DOWN, Label.FLAT, Label.UP]
    raw, labels = [], []
    for _ in range(3000):
        logits = [rng.gauss(0, 1) for _ in range(3)]
        p = softmax(logits)
        labels.append(rng.choices(classes, weights=p)[0])
        raw.append([3 * x for x in logits])
    t = fit_temperature(raw, labels)
    assert 2.5 < t < 3.5
    assert nll(raw, labels, t) < nll(raw, labels, 1.0)


@pytest.fixture(scope="module")
def data() -> tuple[list, list]:  # type: ignore[type-arg]
    candles = candle_series(N, seed=3)
    funding = funding_series(T0 - 30 * HOUR, T0 + (N + 5) * HOUR, seed=5)
    return candles, funding


@pytest.fixture(scope="module")
def dataset_dir(tmp_path_factory: pytest.TempPathFactory, data: tuple[list, list]) -> Path:  # type: ignore[type-arg]
    candles, funding = data
    target = TargetParams(4, STRATEGY.target.threshold_bps, STRATEGY.target.cost_bps)
    dataset = build_dataset(candles, funding, target)
    parts = chronological_split(dataset.rows, 4 * HOUR)
    out = tmp_path_factory.mktemp("dataset")
    write_dataset(dataset, parts, out, market={"coin": "BTC"}, project_root=ROOT)
    return out


@pytest.fixture(scope="module")
def artifact_dir(tmp_path_factory: pytest.TempPathFactory, dataset_dir: Path) -> Path:
    root = tmp_path_factory.mktemp("models")
    return train_model(dataset_dir, STRATEGY, PARAMS, root, catboost_params=FAST).path


def test_artifact_contents(artifact_dir: Path) -> None:
    artifact = load_artifact(artifact_dir)
    meta = artifact.meta
    assert artifact.version.startswith("cb-v1-") and artifact.feature_names == FEATURE_NAMES
    assert meta["target"]["threshold_bps"] == "39.5"
    assert set(meta["metrics"]) == {"validation", "calibration", "test"}
    test = meta["metrics"]["test"]
    for key in ("log_loss", "brier", "ece", "baseline_log_loss", "policy"):
        assert key in test
    assert meta["calibration"]["temperature"] > 0
    assert set(meta["feature_bounds"]) == set(FEATURE_NAMES)
    assert (
        meta["hashes"]["dataset_sha256"]
        == json.loads((Path(meta["dataset_dir"]) / "manifest.json").read_text())["dataset_sha256"]
    )
    assert len(meta["threshold_sweep_validation"]) == 5


def test_training_is_deterministic(tmp_path: Path, dataset_dir: Path, artifact_dir: Path) -> None:
    again = train_model(dataset_dir, STRATEGY, PARAMS, tmp_path, catboost_params=FAST)
    first = load_artifact(artifact_dir)
    # файл .cbm отличается метаданными (model_guid, время), сама модель — нет
    assert again.meta["hashes"]["model_fingerprint"] == first.meta["hashes"]["model_fingerprint"]
    assert again.meta["metrics"] == first.meta["metrics"]


def test_prediction_is_reproducible(artifact_dir: Path, data: tuple[list, list]) -> None:  # type: ignore[type-arg]
    candles, funding = data
    i = N - 30
    known = [f for f in funding if f.time < candles[i].close_time]
    _, vector = features_for(candles[: i + 1], known)
    p1 = Predictor(load_artifact(artifact_dir)).predict(vector)
    p2 = Predictor(load_artifact(artifact_dir)).predict(vector)  # новая загрузка с диска
    assert p1 == p2
    assert math.fsum(p1.probabilities.values()) == pytest.approx(1.0)
    assert list(p1.probabilities) == ["DOWN", "FLAT", "UP"]


def test_out_of_bounds_feature_rejected(artifact_dir: Path, data: tuple[list, list]) -> None:  # type: ignore[type-arg]
    candles, funding = data
    known = [f for f in funding if f.time < candles[-1].close_time]
    _, vector = features_for(candles, known)
    values = list(vector.values)
    values[FEATURE_NAMES.index("rv_24")] = 10.0  # 1000% часовой волатильности
    broken = dataclasses.replace(vector, values=tuple(values))
    with pytest.raises(OutOfBoundsError, match="rv_24"):
        Predictor(load_artifact(artifact_dir)).predict(broken)


def test_tampered_artifact_rejected(tmp_path: Path, artifact_dir: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(artifact_dir, copy)
    with (copy / "model.cbm").open("ab") as f:
        f.write(b"\0")
    with pytest.raises(ArtifactError, match="хеш"):
        load_artifact(copy)


def test_pipeline_change_rejected(tmp_path: Path, artifact_dir: Path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(artifact_dir, copy)
    meta = json.loads((copy / "artifact.json").read_text())
    meta["pipeline_code_sha256"] = "0" * 64
    (copy / "artifact.json").write_text(json.dumps(meta))
    with pytest.raises(ArtifactError, match="pipeline"):
        Predictor(load_artifact(copy))


def test_dataset_integrity_checked(tmp_path: Path, dataset_dir: Path) -> None:
    copy = tmp_path / "ds"
    shutil.copytree(dataset_dir, copy)
    load_dataset_dir(copy)
    with (copy / "dataset.csv").open("a") as f:
        f.write("x\n")
    with pytest.raises(TrainingError, match="хеш"):
        load_dataset_dir(copy)


def test_bounds_padding_is_relative(artifact_dir: Path) -> None:
    bounds = load_artifact(artifact_dir).bounds
    b = bounds["hour_sin"]
    assert b.low < -1 and b.high > 1  # диапазон train [-1, 1] расширен
    assert timedelta(hours=1) == HOUR
