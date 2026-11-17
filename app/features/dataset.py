"""Обучающий набор (§14.2–14.3): признаки + target, хронологическое разбиение, экспорт.

Строка набора — момент решения t (конец закрытой свечи): признаки по окну до t и класс
доходности от close(t) до close(t + horizon). Перемешивания нет: train — самый ранний
участок, затем validation, calibration и test. Между участками выбрасывается промежуток
длиной в горизонт (purge): метка последней строки train «видит» цены начала validation.
"""

import bisect
import hashlib
import json
import math
import shutil
import subprocess
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import fmean
from typing import Any

from app.domain.market import Candle, FundingPoint
from app.features.pipeline import (
    FEATURE_NAMES,
    FEATURE_SET_VERSION,
    FUNDING_WINDOW,
    LOOKBACK_CANDLES,
    FeatureError,
    features_for,
    pipeline_code_hash,
)
from app.features.target import CLASSES, Label, forward_return, label_for
from app.models.metrics import class_distribution, prior_baseline

SPLIT_NAMES = ("train", "validation", "calibration", "test")
DEFAULT_FRACTIONS = (0.6, 0.15, 0.1, 0.15)
DATASET_FORMAT = 1


@dataclass(frozen=True, slots=True)
class DatasetRow:
    decision_time: datetime
    features: tuple[float, ...]
    fwd_return: float
    label: Label


@dataclass(frozen=True, slots=True)
class TargetParams:
    horizon_candles: int
    threshold_bps: Decimal
    cost_bps: Decimal

    def as_dict(self) -> dict[str, Any]:
        return {
            "horizon_candles": self.horizon_candles,
            "threshold_bps": str(self.threshold_bps),
            "cost_bps": str(self.cost_bps),
            "entry": "close свечи решения",
            "exit": "close через horizon свечей",
        }


@dataclass
class Dataset:
    target: TargetParams
    rows: list[DatasetRow]
    skipped: Counter[str] = field(default_factory=Counter)
    source_candles: int = 0
    source_funding: int = 0
    source_hash: str = ""

    @property
    def labels(self) -> list[Label]:
        return [r.label for r in self.rows]


def _skip_reason(exc: FeatureError) -> str:
    text = str(exc)
    for key in ("пропуск свечей", "funding", "не закрыта", "объёма", "нулевая волатильность"):
        if key in text:
            return key
    return "другое"


def source_hash(candles: Sequence[Candle], funding: Sequence[FundingPoint]) -> str:
    """Хеш исходных данных набора (все свечи и funding, по которым он построен)."""
    digest = hashlib.sha256()
    for c in candles:
        digest.update(
            f"c|{c.open_time.isoformat()}|{c.open}|{c.high}|{c.low}|{c.close}|"
            f"{c.volume}|{c.trades_count}\n".encode()
        )
    for f in funding:
        digest.update(f"f|{f.time.isoformat()}|{f.funding_rate}|{f.premium}\n".encode())
    return digest.hexdigest()


def build_dataset(
    candles: Sequence[Candle],
    funding: Sequence[FundingPoint],
    target: TargetParams,
    *,
    step: timedelta = timedelta(hours=1),
) -> Dataset:
    """candles — закрытые свечи по возрастанию open_time; funding — по возрастанию времени."""
    candles = [c for c in candles if c.is_closed]
    funding_times = [f.time for f in funding]
    horizon = target.horizon_candles
    dataset = Dataset(
        target=target,
        rows=[],
        source_candles=len(candles),
        source_funding=len(funding),
        source_hash=source_hash(candles, funding),
    )
    for i in range(LOOKBACK_CANDLES - 1, len(candles) - horizon):
        current, future = candles[i], candles[i + horizon]
        if future.open_time - current.open_time != horizon * step:
            dataset.skipped["пропуск в горизонте"] += 1
            continue
        # funding — с небольшим запасом до момента решения; лишнее отрежет prepare_input
        end = bisect.bisect_left(funding_times, current.close_time)
        window_funding = funding[max(0, end - FUNDING_WINDOW - 2) : end]
        try:
            _, vector = features_for(
                candles[i - LOOKBACK_CANDLES + 1 : i + 1], window_funding, step=step
            )
        except FeatureError as exc:
            dataset.skipped[_skip_reason(exc)] += 1
            continue
        ret = forward_return(current.close, future.close)
        dataset.rows.append(
            DatasetRow(
                decision_time=vector.decision_time,
                features=vector.values,
                fwd_return=ret,
                label=label_for(ret, target.threshold_bps),
            )
        )
    return dataset


# --- разбиение ---


@dataclass(frozen=True, slots=True)
class SplitPart:
    name: str
    rows: list[DatasetRow]

    @property
    def start(self) -> datetime | None:
        return self.rows[0].decision_time if self.rows else None

    @property
    def end(self) -> datetime | None:
        return self.rows[-1].decision_time if self.rows else None


def chronological_split(
    rows: Sequence[DatasetRow],
    horizon: timedelta,
    fractions: Sequence[float] = DEFAULT_FRACTIONS,
) -> list[SplitPart]:
    """Делит строки по времени в пропорции fractions. Каждый следующий участок начинается
    не раньше чем через horizon после последней строки предыдущего (purge)."""
    if len(fractions) != len(SPLIT_NAMES) or any(f <= 0 for f in fractions):
        raise ValueError(f"нужно {len(SPLIT_NAMES)} положительных долей")
    if not math.isclose(sum(fractions), 1.0):
        raise ValueError("сумма долей должна быть 1")
    n = len(rows)
    cuts, acc = [], 0.0
    for f in fractions[:-1]:
        acc += f
        cuts.append(round(n * acc))
    bounds = [0, *cuts, n]
    parts: list[SplitPart] = []
    previous_end: datetime | None = None
    for name, lo, hi in zip(SPLIT_NAMES, bounds, bounds[1:], strict=False):
        chunk = list(rows[lo:hi])
        if previous_end is not None:
            chunk = [r for r in chunk if r.decision_time >= previous_end + horizon]
        parts.append(SplitPart(name, chunk))
        if chunk:
            previous_end = chunk[-1].decision_time
    return parts


# --- экспорт и отчёт ---


def _code_version(root: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return "unknown"
    try:
        commit = subprocess.run(  # noqa: S603 — фиксированные аргументы
            [git, "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(  # noqa: S603
            [git, "status", "--porcelain", "--", "app"],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return f"{commit}{'-dirty' if dirty else ''}"


def split_report(parts: Sequence[SplitPart]) -> dict[str, Any]:
    """Распределение классов, средняя будущая доходность по классу и baseline по участкам."""
    train = next(p for p in parts if p.name == "train")
    report: dict[str, Any] = {}
    for part in parts:
        labels = [r.label for r in part.rows]
        by_class = {
            c.value: fmean([r.fwd_return for r in part.rows if r.label == c])
            for c in CLASSES
            if any(r.label == c for r in part.rows)
        }
        entry: dict[str, Any] = {
            "rows": len(part.rows),
            "start": part.start.isoformat() if part.start else None,
            "end": part.end.isoformat() if part.end else None,
            "class_distribution": class_distribution(labels),
            "mean_fwd_return_by_class": by_class,
        }
        if part.name != "train" and labels and train.rows:
            base = prior_baseline([r.label for r in train.rows], labels)
            entry["baseline_prior"] = {"log_loss": base.log_loss, "brier": base.brier}
        report[part.name] = entry
    return report


def write_dataset(
    dataset: Dataset,
    parts: Sequence[SplitPart],
    out_dir: Path,
    *,
    market: dict[str, Any],
    project_root: Path,
) -> tuple[Path, dict[str, Any]]:
    """Пишет dataset.csv и manifest.json. CSV детерминирован: одинаковые данные — одинаковый хеш."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "dataset.csv"
    split_of = {id(r): p.name for p in parts for r in p.rows}
    lines = [",".join(["decision_time", "split", "label", "fwd_return", *FEATURE_NAMES])]
    for row in dataset.rows:
        split = split_of.get(id(row), "purged")
        values = [repr(x) for x in row.features]
        lines.append(
            ",".join(
                [
                    row.decision_time.isoformat(),
                    split,
                    row.label.value,
                    repr(row.fwd_return),
                    *values,
                ]
            )
        )
    content = ("\n".join(lines) + "\n").encode()
    csv_path.write_bytes(content)
    manifest = {
        "format": DATASET_FORMAT,
        "created_at": datetime.now(UTC).isoformat(),
        "market": market,
        "feature_set_version": FEATURE_SET_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "pipeline_code_sha256": pipeline_code_hash(),
        "code_version": _code_version(project_root),
        "target": dataset.target.as_dict(),
        "classes": [c.value for c in CLASSES],
        "source": {
            "candles": dataset.source_candles,
            "funding_points": dataset.source_funding,
            "sha256": dataset.source_hash,
        },
        "rows": len(dataset.rows),
        "skipped": dict(dataset.skipped),
        "purged_rows": sum(1 for r in dataset.rows if id(r) not in split_of),
        "splits": split_report(parts),
        "dataset_sha256": hashlib.sha256(content).hexdigest(),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return csv_path, manifest
