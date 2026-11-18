"""Обучение CatBoost (§14.3–14.4, §15).

train → обучение, validation → ранняя остановка и выбор итерации, calibration → температура,
test → итоговая проверка (участок не используется ни для одного выбора).
Обучение детерминировано: фиксированные seed и число потоков.
"""

import csv
import json
import math
import shutil
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import fmean
from typing import Any

from catboost import CatBoostClassifier

from app.config.strategy import StrategyConfig
from app.features.pipeline import FEATURE_NAMES, FEATURE_SET_VERSION, pipeline_code_hash
from app.features.target import CLASSES, Label
from app.models.artifact import (
    ARTIFACT_FORMAT,
    MODEL_FILE,
    ModelArtifact,
    model_fingerprint,
    sha256_file,
    write_artifact,
)
from app.models.calibration import fit_temperature, softmax
from app.models.metrics import brier, class_distribution, log_loss, prior_baseline
from app.models.predictor import Prediction
from app.signals.policy import PolicyParams, model_direction

CATBOOST_PARAMS: dict[str, Any] = {
    "loss_function": "MultiClass",
    "iterations": 3000,
    "learning_rate": 0.03,
    "depth": 4,
    "l2_leaf_reg": 10,
    "random_seed": 42,
    "od_type": "Iter",
    "od_wait": 200,
    "use_best_model": True,
    "thread_count": 4,
    "allow_writing_files": False,
    "verbose": False,
}
BOUNDS_PADDING = 0.25  # доля диапазона train, на которую расширяются границы признаков
SWEEP_PROBABILITIES = (0.40, 0.45, 0.50, 0.55, 0.60)


class TrainingError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Sample:
    decision_time: datetime
    label: Label
    fwd_return: float
    features: tuple[float, ...]
    source: str = "hyperliquid"


def load_dataset_dir(path: Path) -> tuple[dict[str, Any], dict[str, list[Sample]]]:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("feature_set_version") != FEATURE_SET_VERSION:
        raise TrainingError(
            f"набор построен признаками {manifest.get('feature_set_version')}, "
            f"код — {FEATURE_SET_VERSION}"
        )
    if manifest.get("pipeline_code_sha256") != pipeline_code_hash():
        raise TrainingError("набор построен другой версией кода pipeline — пересоберите его")
    if manifest.get("feature_names") != list(FEATURE_NAMES):
        raise TrainingError("состав признаков набора не совпадает с pipeline")
    csv_path = path / "dataset.csv"
    if sha256_file(csv_path) != manifest["dataset_sha256"]:
        raise TrainingError("dataset.csv не совпадает с хешем в manifest.json")
    parts: dict[str, list[Sample]] = {}
    with csv_path.open(newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        first = 5 if manifest.get("format", 1) >= 2 else 4  # формат 2: столбец source
        if header[first:] != list(FEATURE_NAMES):
            raise TrainingError("заголовок dataset.csv не совпадает с признаками")
        for row in reader:
            parts.setdefault(row[1], []).append(
                Sample(
                    decision_time=datetime.fromisoformat(row[0]),
                    label=Label(row[2]),
                    fwd_return=float(row[3]),
                    features=tuple(float(x) for x in row[first:]),
                    source=row[4] if first == 5 else "hyperliquid",
                )
            )
    for name in ("train", "validation", "calibration", "test"):
        if not parts.get(name):
            raise TrainingError(f"в наборе нет строк участка {name}")
    return manifest, parts


def _matrix(samples: Sequence[Sample]) -> list[list[float]]:
    return [list(s.features) for s in samples]


def _labels(samples: Sequence[Sample]) -> list[str]:
    return [s.label.value for s in samples]


def expected_calibration_error(
    labels: Sequence[Label], probs: Sequence[Sequence[float]], bins: int = 10
) -> float:
    """ECE по уверенности в top-классе: |средняя уверенность − доля верных| по корзинам."""
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for y, p in zip(labels, probs, strict=True):
        k = max(range(len(p)), key=lambda i: p[i])
        conf = p[k]
        buckets[min(int(conf * bins), bins - 1)].append((conf, CLASSES[k] == y))
    total = len(labels)
    return math.fsum(
        len(b) / total * abs(fmean(c for c, _ in b) - fmean(1.0 if ok else 0.0 for _, ok in b))
        for b in buckets
        if b
    )


def policy_stats(
    samples: Sequence[Sample],
    preds: Sequence[Prediction],
    params: PolicyParams,
    cost: float,
) -> dict[str, Any]:
    """Сигналы политики без учёта позиции (каждая строка независимо) и их итог за горизонт
    минус издержки. Это sanity-check, а не симуляция торговли (§14.3)."""
    longs: list[Sample] = []
    shorts: list[Sample] = []
    for s, pred in zip(samples, preds, strict=True):
        direction, _ = model_direction(pred, params)
        if direction is None:
            continue
        (longs if direction.value == "buy" else shorts).append(s)
    n = len(samples)
    hits = sum(s.label is Label.UP for s in longs) + sum(s.label is Label.DOWN for s in shorts)
    net = [s.fwd_return - cost for s in longs] + [-s.fwd_return - cost for s in shorts]
    return {
        "rows": n,
        "buy_signals": len(longs),
        "sell_signals": len(shorts),
        "signal_rate": (len(longs) + len(shorts)) / n if n else 0.0,
        "precision": hits / len(net) if net else None,
        "mean_net_return": fmean(net) if net else None,
        "sum_net_return": math.fsum(net) if net else 0.0,
    }


def _evaluate(
    samples: Sequence[Sample],
    preds: Sequence[Prediction],
    train_labels: Sequence[Label],
    params: PolicyParams,
    cost: float,
) -> dict[str, Any]:
    labels = [s.label for s in samples]
    probs = [[p.probabilities[c.value] for c in CLASSES] for p in preds]
    base = prior_baseline(train_labels, labels)
    predicted = [CLASSES[max(range(3), key=lambda i: row[i])] for row in probs]
    return {
        "rows": len(samples),
        "log_loss": log_loss(labels, probs),
        "brier": brier(labels, probs),
        "ece": expected_calibration_error(labels, probs),
        "accuracy": fmean(1.0 if a == b else 0.0 for a, b in zip(predicted, labels, strict=True)),
        "baseline_log_loss": base.log_loss,
        "baseline_brier": base.brier,
        "beats_baseline": log_loss(labels, probs) < base.log_loss,
        "class_distribution": class_distribution(labels),
        "predicted_distribution": class_distribution(predicted),
        "policy": policy_stats(samples, preds, params, cost),
    }


def _bounds(train: Sequence[Sample]) -> dict[str, list[float]]:
    result = {}
    for i, name in enumerate(FEATURE_NAMES):
        values = [s.features[i] for s in train]
        lo, hi = min(values), max(values)
        pad = (hi - lo) * BOUNDS_PADDING or 1e-9
        result[name] = [lo - pad, hi + pad]
    return result


def _feature_stats(train: Sequence[Sample]) -> dict[str, dict[str, float]]:
    """Распределение признаков на train: база для контроля дрейфа в shadow."""
    stats = {}
    for i, name in enumerate(FEATURE_NAMES):
        values = sorted(s.features[i] for s in train)
        n = len(values)
        mean = math.fsum(values) / n
        std = math.sqrt(math.fsum((v - mean) ** 2 for v in values) / max(n - 1, 1))
        stats[name] = {
            "mean": mean,
            "std": std,
            "p01": values[int(0.01 * (n - 1))],
            "p50": values[int(0.5 * (n - 1))],
            "p99": values[int(0.99 * (n - 1))],
        }
    return stats


def _predictions(
    raw: Sequence[Sequence[float]],
    temperature: float,
    class_returns: dict[str, float],
    version: str,
) -> list[Prediction]:
    result = []
    for r in raw:
        probs = softmax(r, temperature)
        probabilities = {c.value: p for c, p in zip(CLASSES, probs, strict=True)}
        expected = math.fsum(probabilities[k] * class_returns[k] for k in probabilities)
        result.append(Prediction(version, probabilities, tuple(r), expected))
    return result


def train_model(
    dataset_dir: Path,
    strategy: StrategyConfig,
    params: PolicyParams,
    models_root: Path,
    *,
    catboost_params: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> ModelArtifact:
    manifest, parts = load_dataset_dir(dataset_dir)
    train, val, cal, test = (parts[n] for n in ("train", "validation", "calibration", "test"))
    cb_params = {**CATBOOST_PARAMS, **(catboost_params or {})}
    # Признаки, которые у вспомогательного источника не совпадают с Hyperliquid (сверка на
    # общем периоде), модель не использует: иначе она выучит чужую механику funding/premium.
    ignored = sorted((manifest.get("auxiliary") or {}).get("overlap", {}).get("weak_features", []))
    if ignored:
        cb_params["ignored_features"] = [FEATURE_NAMES.index(name) for name in ignored]
    model = CatBoostClassifier(**cb_params, class_names=[c.value for c in CLASSES])
    model.fit(_matrix(train), _labels(train), eval_set=(_matrix(val), _labels(val)))
    if [str(c) for c in model.classes_] != [c.value for c in CLASSES]:
        raise TrainingError(f"порядок классов CatBoost {list(model.classes_)}")

    def raw(samples: Sequence[Sample]) -> list[list[float]]:
        out = model.predict(_matrix(samples), prediction_type="RawFormulaVal", thread_count=1)
        return [[float(x) for x in row] for row in out]

    temperature = fit_temperature(raw(cal), [s.label for s in cal])
    class_returns = {
        c.value: fmean([s.fwd_return for s in train if s.label is c])
        if any(s.label is c for s in train)
        else 0.0
        for c in CLASSES
    }
    cost = float(strategy.target.cost_bps) / 10_000
    train_labels = [s.label for s in train]

    fingerprint = model_fingerprint(model)
    models_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=models_root) as tmp:
        tmp_model = Path(tmp) / MODEL_FILE
        model.save_model(str(tmp_model))
        model_sha = sha256_file(tmp_model)
        stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M")
        version = f"cb-{FEATURE_SET_VERSION}-{stamp}-{fingerprint[:8]}"
        out_dir = models_root / version
        out_dir.mkdir(parents=True, exist_ok=False)
        shutil.move(str(tmp_model), out_dir / MODEL_FILE)

    def evaluate(samples: Sequence[Sample]) -> dict[str, Any]:
        preds = _predictions(raw(samples), temperature, class_returns, version)
        return _evaluate(samples, preds, train_labels, params, cost)

    val_preds = _predictions(raw(val), temperature, class_returns, version)
    sweep = []
    for p_min in SWEEP_PROBABILITIES:
        swept = replace(params, min_probability=p_min)
        sweep.append({"min_probability": p_min, **policy_stats(val, val_preds, swept, cost)})

    importance = sorted(
        zip(FEATURE_NAMES, (float(x) for x in model.get_feature_importance()), strict=True),
        key=lambda kv: -kv[1],
    )
    meta: dict[str, Any] = {
        "format": ARTIFACT_FORMAT,
        "version": version,
        "created_at": (now or datetime.now(UTC)).isoformat(),
        "model_type": "catboost",
        "classes": [c.value for c in CLASSES],
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "pipeline_code_sha256": pipeline_code_hash(),
        "feature_bounds": _bounds(train),
        "feature_stats": _feature_stats(train),
        "train_class_distribution": class_distribution(train_labels),
        "target": manifest["target"],
        "market": manifest["market"],
        "training_period": {
            name: {
                "start": rows[0].decision_time.isoformat(),
                "end": rows[-1].decision_time.isoformat(),
                "rows": len(rows),
                "rows_by_source": dict(Counter(r.source for r in rows)),
            }
            for name, rows in (
                ("train", train),
                ("validation", val),
                ("calibration", cal),
                ("test", test),
            )
        },
        "catboost": {
            **{k: v for k, v in cb_params.items() if k != "verbose"},
            "best_iteration": int(model.get_best_iteration() or 0),
            "tree_count": int(model.tree_count_),
        },
        "calibration": {"method": "temperature", "temperature": temperature},
        "class_mean_returns": class_returns,
        "policy_at_training": {
            "min_probability": params.min_probability,
            "min_margin": params.min_margin,
            "min_edge_bps": params.min_edge * 1e4,
            "cost_bps": float(strategy.target.cost_bps),
        },
        "metrics": {
            "validation": evaluate(val),
            "calibration": evaluate(cal),
            "test": evaluate(test),
        },
        "threshold_sweep_validation": sweep,
        "feature_importance": dict(importance),
        "hashes": {
            "model_sha256": model_sha,
            "model_fingerprint": fingerprint,
            "dataset_sha256": manifest["dataset_sha256"],
            "source_sha256": manifest["source"]["sha256"],
        },
        "dataset_dir": str(dataset_dir),
        "auxiliary_source": manifest.get("auxiliary"),
        "ignored_features": ignored,
        "code_version": manifest.get("code_version"),
    }
    return write_artifact(out_dir, meta)
