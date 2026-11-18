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
DATASET_FORMAT = 2  # 2: столбец source (hyperliquid | binance)
PRIMARY_SOURCE = "hyperliquid"


@dataclass(frozen=True, slots=True)
class DatasetRow:
    decision_time: datetime
    features: tuple[float, ...]
    fwd_return: float
    label: Label
    source: str = PRIMARY_SOURCE


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
    auxiliary: dict[str, Any] | None = None  # вспомогательный источник (§7.4), если добавлен

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


@dataclass(frozen=True, slots=True)
class FeatureRow:
    """Признаки на момент решения: index — номер свечи решения в переданном списке."""

    index: int
    decision_time: datetime
    features: tuple[float, ...]


def feature_rows(
    candles: Sequence[Candle],
    funding: Sequence[FundingPoint],
    *,
    horizon: int,
    step: timedelta = timedelta(hours=1),
) -> tuple[list[FeatureRow], Counter[str]]:
    """Признаки для каждой свечи, у которой есть полная история и horizon свечей после
    (без пропусков). candles — только закрытые, по возрастанию open_time."""
    funding_times = [f.time for f in funding]
    rows: list[FeatureRow] = []
    skipped: Counter[str] = Counter()
    for i in range(LOOKBACK_CANDLES - 1, len(candles) - horizon):
        current, future = candles[i], candles[i + horizon]
        if future.open_time - current.open_time != horizon * step:
            skipped["пропуск в горизонте"] += 1
            continue
        # funding — с небольшим запасом до момента решения; лишнее отрежет prepare_input
        end = bisect.bisect_left(funding_times, current.close_time)
        window_funding = funding[max(0, end - FUNDING_WINDOW - 2) : end]
        try:
            _, vector = features_for(
                candles[i - LOOKBACK_CANDLES + 1 : i + 1], window_funding, step=step
            )
        except FeatureError as exc:
            skipped[_skip_reason(exc)] += 1
            continue
        rows.append(FeatureRow(i, vector.decision_time, vector.values))
    return rows, skipped


def build_dataset(
    candles: Sequence[Candle],
    funding: Sequence[FundingPoint],
    target: TargetParams,
    *,
    step: timedelta = timedelta(hours=1),
    source: str = PRIMARY_SOURCE,
) -> Dataset:
    """candles — закрытые свечи по возрастанию open_time; funding — по возрастанию времени."""
    candles = [c for c in candles if c.is_closed]
    horizon = target.horizon_candles
    rows, skipped = feature_rows(candles, funding, horizon=horizon, step=step)
    dataset = Dataset(
        target=target,
        rows=[],
        skipped=skipped,
        source_candles=len(candles),
        source_funding=len(funding),
        source_hash=source_hash(candles, funding),
    )
    for row in rows:
        ret = forward_return(candles[row.index].close, candles[row.index + horizon].close)
        dataset.rows.append(
            DatasetRow(
                decision_time=row.decision_time,
                features=row.features,
                fwd_return=ret,
                label=label_for(ret, target.threshold_bps),
                source=source,
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
    lines = [",".join(["decision_time", "split", "label", "fwd_return", "source", *FEATURE_NAMES])]
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
                    row.source,
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
        "rows_by_source": dict(Counter(r.source for r in dataset.rows)),
        "auxiliary": dataset.auxiliary,
        "skipped": dict(dataset.skipped),
        "purged_rows": sum(1 for r in dataset.rows if id(r) not in split_of),
        "splits": split_report(parts),
        "dataset_sha256": hashlib.sha256(content).hexdigest(),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return csv_path, manifest


# --- вспомогательный источник (§7.4) ---


def _corr(xs: Sequence[float], ys: Sequence[float]) -> float | None:
    if len(xs) < 3:
        return None
    mx, my = fmean(xs), fmean(ys)
    sxx = math.fsum((x - mx) ** 2 for x in xs)
    syy = math.fsum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return math.fsum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / math.sqrt(
        sxx * syy
    )


def compare_sources(
    primary: Sequence[DatasetRow], auxiliary: Sequence[DatasetRow]
) -> dict[str, Any]:
    """Совпадение источников на общем периоде: метки, доходность за горизонт и признаки.
    Признак считается согласованным, если корреляция ≥ 0.8 и сдвиг среднего < 0.5σ."""
    aux_by_time = {r.decision_time: r for r in auxiliary}
    pairs = [(p, aux_by_time[p.decision_time]) for p in primary if p.decision_time in aux_by_time]
    if not pairs:
        return {"overlap_rows": 0}
    features = {}
    for i, name in enumerate(FEATURE_NAMES):
        a = [p.features[i] for p, _ in pairs]
        b = [x.features[i] for _, x in pairs]
        std = math.sqrt(math.fsum((v - fmean(a)) ** 2 for v in a) / max(len(a) - 1, 1))
        features[name] = {
            "corr": _corr(a, b),
            "mean_shift_sigma": (fmean(b) - fmean(a)) / std if std > 0 else None,
        }
    weak = sorted(
        name
        for name, f in features.items()
        if (f["corr"] is not None and f["corr"] < 0.8)
        or (f["mean_shift_sigma"] is not None and abs(f["mean_shift_sigma"]) >= 0.5)
    )
    return {
        "overlap_rows": len(pairs),
        "label_agreement": sum(p.label is x.label for p, x in pairs) / len(pairs),
        "fwd_return_corr": _corr(
            [p.fwd_return for p, _ in pairs], [x.fwd_return for _, x in pairs]
        ),
        "features": features,
        "weak_features": weak,
    }


def with_auxiliary(
    dataset: Dataset,
    parts: list[SplitPart],
    aux: Dataset,
    *,
    horizon: timedelta,
    name: str,
) -> tuple[Dataset, list[SplitPart]]:
    """Добавляет строки вспомогательного источника только в train и только раньше начала
    основной истории (с purge-промежутком). validation / calibration / test остаются
    строками основного источника — модель оценивается на той бирже, где торгует."""
    if not dataset.rows:
        return dataset, parts
    first_primary = dataset.rows[0].decision_time
    early = [r for r in aux.rows if r.decision_time <= first_primary - horizon]
    overlap = compare_sources(dataset.rows, aux.rows)
    combined = Dataset(
        target=dataset.target,
        rows=early + dataset.rows,
        skipped=dataset.skipped,
        source_candles=dataset.source_candles + aux.source_candles,
        source_funding=dataset.source_funding + aux.source_funding,
        source_hash=hashlib.sha256((dataset.source_hash + aux.source_hash).encode()).hexdigest(),
        auxiliary={
            "name": name,
            "rows_in_train": len(early),
            "start": early[0].decision_time.isoformat() if early else None,
            "end": early[-1].decision_time.isoformat() if early else None,
            "source_sha256": aux.source_hash,
            "skipped": dict(aux.skipped),
            "overlap": overlap,
        },
    )
    new_parts = [SplitPart(p.name, early + p.rows) if p.name == "train" else p for p in parts]
    return combined, new_parts
