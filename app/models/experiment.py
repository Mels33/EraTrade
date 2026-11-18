"""Эксперимент с целевой переменной: какая метка даёт полезные сигналы.

Все варианты обучаются на одних и тех же строках и признаках (v1), с тем же разбиением:
train — Hyperliquid + Binance (до начала истории Hyperliquid), validation / calibration /
test — только Hyperliquid. Для каждого варианта порог уверенности подбирается на
validation под желаемую частоту сделок; test используется только для подтверждения.

Это оценка сигналов, а не симуляция исполнения: вход и выход по close/барьеру без
проскальзывания сверх заложенных издержек, без влияния ордера на рынок.
"""

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import fmean
from typing import Any

from catboost import CatBoostClassifier

from app.domain.market import Candle, FundingPoint
from app.features.dataset import (
    DatasetRow,
    FeatureRow,
    chronological_split,
    compare_sources,
    feature_rows,
)
from app.features.pipeline import FEATURE_NAMES
from app.features.target import (
    CLASSES,
    Label,
    Outcome,
    TargetSpec,
    label_barrier,
    label_fixed,
    label_vol,
)
from app.models.calibration import fit_temperature, softmax
from app.models.metrics import class_distribution, log_loss
from app.models.training import CATBOOST_PARAMS

HOUR = timedelta(hours=1)
RV_24 = FEATURE_NAMES.index("rv_24")
ATR_REL = FEATURE_NAMES.index("atr_14_rel")
VOL_FLAT_SHARE = 0.4  # k для vol-меток: ≈40% строк train — FLAT
THETAS = [round(0.34 + 0.01 * i, 2) for i in range(47)]  # 0.34 … 0.80

DEFAULT_VARIANTS: dict[str, TargetSpec] = {
    "fixed4": TargetSpec("fixed4", "fixed", 4),
    "vol4": TargetSpec("vol4", "vol", 4),
    "vol8": TargetSpec("vol8", "vol", 8),
    "barrier8": TargetSpec("barrier8", "barrier", 8),
}


@dataclass(frozen=True, slots=True)
class ExpRow:
    decision_time: datetime
    features: tuple[float, ...]
    index: int  # номер свечи решения в списке своего источника
    source: str
    split: str


def outcome_for(spec: TargetSpec, candles: Sequence[Candle], row: ExpRow) -> Outcome:
    if spec.kind == "fixed":
        return label_fixed(candles, row.index, spec.horizon, spec.threshold_bps)
    if spec.kind == "vol":
        return label_vol(
            candles,
            row.index,
            spec.horizon,
            sigma_1h=row.features[RV_24],
            k=spec.k,
            floor_bps=spec.floor_bps,
        )
    distance = max(spec.atr_mult * row.features[ATR_REL], spec.min_distance)
    return label_barrier(candles, row.index, spec.horizon, distance=distance)


def calibrate_k(
    spec: TargetSpec, rows: Sequence[ExpRow], candles: Mapping[str, Sequence[Candle]]
) -> float:
    """k такой, чтобы ≈VOL_FLAT_SHARE строк train были FLAT (|r| ≤ k·σ·√h)."""
    z = sorted(
        abs(
            float(
                candles[r.source][r.index + spec.horizon].close / candles[r.source][r.index].close
                - 1
            )
        )
        / (r.features[RV_24] * math.sqrt(spec.horizon))
        for r in rows
        if r.features[RV_24] > 0
    )
    return z[int(VOL_FLAT_SHARE * (len(z) - 1))]


@dataclass(frozen=True, slots=True)
class Trade:
    time: datetime
    side: str
    net: float


def select_trades(
    rows: Sequence[ExpRow],
    outcomes: Sequence[Outcome],
    probs: Sequence[Sequence[float]],
    *,
    theta: float,
    mean_long: dict[Label, float],
    mean_short: dict[Label, float],
    edge: float,
    cost: float,
    horizon: int,
) -> list[Trade]:
    """Сигналы политики без наложения: пока сделка открыта (horizon часов), новые не берём."""
    trades: list[Trade] = []
    busy_until: datetime | None = None
    for row, out, p in zip(rows, outcomes, probs, strict=True):
        if busy_until is not None and row.decision_time < busy_until:
            continue
        pr = dict(zip(CLASSES, p, strict=True))
        e_long = math.fsum(pr[c] * mean_long[c] for c in CLASSES)
        e_short = math.fsum(pr[c] * mean_short[c] for c in CLASSES)
        if pr[Label.UP] >= theta and pr[Label.UP] > pr[Label.DOWN] and e_long > edge:
            trades.append(Trade(row.decision_time, "long", out.long_return - cost))
        elif pr[Label.DOWN] >= theta and pr[Label.DOWN] > pr[Label.UP] and e_short > edge:
            trades.append(Trade(row.decision_time, "short", out.short_return - cost))
        else:
            continue
        busy_until = row.decision_time + horizon * HOUR
    return trades


def _days(rows: Sequence[ExpRow]) -> float:
    return max((rows[-1].decision_time - rows[0].decision_time).total_seconds() / 86_400, 1e-9)


def trade_stats(trades: Sequence[Trade], days: float) -> dict[str, Any]:
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t.net <= 0 else 0
        worst = max(worst, streak)
    nets = [t.net for t in trades]
    return {
        "trades": len(trades),
        "per_day": len(trades) / days,
        "hit_rate": sum(n > 0 for n in nets) / len(nets) if nets else None,
        "mean_net": fmean(nets) if nets else None,
        "sum_net": math.fsum(nets),
        "worst_losing_streak": worst,
        "long_share": sum(t.side == "long" for t in trades) / len(trades) if trades else None,
    }


def choose_theta(curve: Sequence[dict[str, Any]], trades_per_day: float) -> dict[str, Any] | None:
    """θ с частотой ближе всего к желаемой (при равенстве — выше, т.е. осторожнее)."""
    candidates = [c for c in curve if c["trades"] > 0]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (abs(c["per_day"] - trades_per_day), -c["theta"]))


def run_experiment(
    primary: tuple[Sequence[Candle], Sequence[FundingPoint]],
    auxiliary: tuple[Sequence[Candle], Sequence[FundingPoint]] | None,
    variants: Sequence[TargetSpec],
    *,
    cost_bps: Decimal,
    edge_bps: Decimal,
    trades_per_day: float,
    fractions: Sequence[float] = (0.6, 0.15, 0.1, 0.15),
    catboost_params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    max_h = max(v.horizon for v in variants)
    purge = max_h * HOUR
    candles = {"hyperliquid": [c for c in primary[0] if c.is_closed]}
    hl_rows, skipped = feature_rows(candles["hyperliquid"], primary[1], horizon=max_h)
    if not hl_rows:
        raise ValueError("нет строк Hyperliquid")

    # разбиение по строкам Hyperliquid (метка для разбиения не нужна)
    stub = [DatasetRow(r.decision_time, r.features, 0.0, Label.FLAT) for r in hl_rows]
    split_of = {
        row.decision_time: part.name
        for part in chronological_split(stub, purge, fractions)
        for row in part.rows
    }
    rows = [
        ExpRow(r.decision_time, r.features, r.index, "hyperliquid", split_of[r.decision_time])
        for r in hl_rows
        if r.decision_time in split_of
    ]

    ignored: list[str] = []
    aux_info: dict[str, Any] | None = None
    if auxiliary is not None:
        candles["binance"] = [c for c in auxiliary[0] if c.is_closed]
        aux_rows: list[FeatureRow]
        aux_rows, _ = feature_rows(candles["binance"], auxiliary[1], horizon=max_h)
        aux_stub = [DatasetRow(r.decision_time, r.features, 0.0, Label.FLAT) for r in aux_rows]
        overlap = compare_sources(stub, aux_stub)
        ignored = sorted(overlap.get("weak_features", []))
        first = hl_rows[0].decision_time
        early = [
            ExpRow(r.decision_time, r.features, r.index, "binance", "train")
            for r in aux_rows
            if r.decision_time <= first - purge
        ]
        rows = early + rows
        aux_info = {"rows_in_train": len(early), "ignored_features": ignored}

    by_split: dict[str, list[ExpRow]] = {}
    for r in rows:
        by_split.setdefault(r.split, []).append(r)
    cost = float(cost_bps) / 10_000
    edge = float(edge_bps) / 10_000
    cb_params = {**CATBOOST_PARAMS, **(catboost_params or {})}
    if ignored:
        cb_params["ignored_features"] = [FEATURE_NAMES.index(n) for n in ignored]

    results = []
    for base_spec in variants:
        spec = base_spec
        if spec.kind == "vol":
            spec = replace(spec, k=calibrate_k(spec, by_split["train"], candles))
        outcomes = {
            name: [outcome_for(spec, candles[r.source], r) for r in part]
            for name, part in by_split.items()
        }
        labels = {name: [o.label.value for o in outs] for name, outs in outcomes.items()}

        model = CatBoostClassifier(**cb_params, class_names=[c.value for c in CLASSES])
        model.fit(
            [list(r.features) for r in by_split["train"]],
            labels["train"],
            eval_set=([list(r.features) for r in by_split["validation"]], labels["validation"]),
        )

        def raw(name: str, m: CatBoostClassifier = model) -> list[list[float]]:
            out = m.predict(
                [list(r.features) for r in by_split[name]],
                prediction_type="RawFormulaVal",
                thread_count=1,
            )
            return [[float(x) for x in row] for row in out]

        cal_labels = [o.label for o in outcomes["calibration"]]
        temperature = fit_temperature(raw("calibration"), cal_labels)
        train_out = outcomes["train"]
        mean_long = {
            c: fmean([o.long_return for o in train_out if o.label is c] or [0.0]) for c in CLASSES
        }
        mean_short = {
            c: fmean([o.short_return for o in train_out if o.label is c] or [0.0]) for c in CLASSES
        }
        prior = class_distribution([o.label for o in train_out])

        segments: dict[str, Any] = {}
        curve: list[dict[str, Any]] = []
        probs_by: dict[str, list[list[float]]] = {}
        for name in ("validation", "test"):
            probs = [list(softmax(r, temperature)) for r in raw(name)]
            probs_by[name] = probs
            seg_labels = [o.label for o in outcomes[name]]
            ll = log_loss(seg_labels, probs)
            base = log_loss(seg_labels, [[prior[c.value] for c in CLASSES]] * len(seg_labels))
            segments[name] = {
                "rows": len(seg_labels),
                "class_distribution": class_distribution(seg_labels),
                "log_loss": ll,
                "baseline_log_loss": base,
                "log_loss_gain": 1 - ll / base if base else None,
                "max_directional_p90": sorted(max(p[0], p[2]) for p in probs)[
                    int(0.9 * (len(probs) - 1))
                ],
            }

        days_val = _days(by_split["validation"])
        for theta in THETAS:
            trades = select_trades(
                by_split["validation"],
                outcomes["validation"],
                probs_by["validation"],
                theta=theta,
                mean_long=mean_long,
                mean_short=mean_short,
                edge=edge,
                cost=cost,
                horizon=spec.horizon,
            )
            curve.append({"theta": theta, **trade_stats(trades, days_val)})
        chosen = choose_theta(curve, trades_per_day)
        if chosen is not None:
            for name in ("validation", "test"):
                trades = select_trades(
                    by_split[name],
                    outcomes[name],
                    probs_by[name],
                    theta=chosen["theta"],
                    mean_long=mean_long,
                    mean_short=mean_short,
                    edge=edge,
                    cost=cost,
                    horizon=spec.horizon,
                )
                segments[name]["trading"] = trade_stats(trades, _days(by_split[name]))
        results.append(
            {
                "variant": spec.name,
                "kind": spec.kind,
                "horizon": spec.horizon,
                "k": spec.k if spec.kind == "vol" else None,
                "train_class_distribution": prior,
                "best_iteration": int(model.get_best_iteration() or 0),
                "temperature": temperature,
                "theta": chosen["theta"] if chosen else None,
                "segments": segments,
                "theta_curve_validation": curve,
            }
        )

    return {
        "rows": dict(Counter(r.split for r in rows)),
        "rows_by_source": dict(Counter(r.source for r in rows)),
        "skipped_primary": dict(skipped),
        "auxiliary": aux_info,
        "cost_bps": float(cost_bps),
        "edge_bps": float(edge_bps),
        "trades_per_day_target": trades_per_day,
        "note": (
            "Оценка сигналов, а не симуляция исполнения: без проскальзывания сверх "
            "заложенных издержек и без влияния ордера на рынок; одна позиция за раз."
        ),
        "variants": results,
    }
