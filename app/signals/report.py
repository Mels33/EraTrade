"""Отчёт mainnet shadow (§16.4): по каждой версии модели за период.

- покрытие: на сколько закрытых свечей есть сигнал, сколько из них — HOLD из-за ошибки;
- частота решений и распределение вероятностей;
- калибровка по накопленным результатам: log loss / Brier / ECE против априорного baseline;
- итог направленных сигналов за горизонт минус издержки (sanity-check, не торговля);
- задержки решения, спред и проскальзывание против заложенных в target;
- отказы risk engine / guard;
- стабильность признаков: сдвиг среднего в σ train и доля значений вне границ обучения.
"""

import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FeatureSnapshotRecord, ModelVersionRecord, SignalRecord
from app.features.target import CLASSES, Label
from app.models.metrics import brier, log_loss

MIN_OUTCOMES_FOR_METRICS = 20
DRIFT_SIGMA_WARN = 1.0


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * (len(ordered) - 1) + 0.5))]


def _ece(labels: Sequence[Label], probs: Sequence[Sequence[float]], bins: int = 10) -> float:
    buckets: list[list[tuple[float, bool]]] = [[] for _ in range(bins)]
    for y, p in zip(labels, probs, strict=True):
        k = max(range(3), key=lambda i: p[i])
        buckets[min(int(p[k] * bins), bins - 1)].append((p[k], CLASSES[k] == y))
    return math.fsum(
        len(b)
        / len(labels)
        * abs(fmean(c for c, _ in b) - fmean(1.0 if ok else 0.0 for _, ok in b))
        for b in buckets
        if b
    )


def _error_kind(reason: str) -> str:
    text = reason.removeprefix("HOLD из-за ошибки: ")
    return text.split(":")[0].strip()[:60] or "другое"


def _model_section(
    model: ModelVersionRecord,
    signals: Sequence[SignalRecord],
    *,
    cost: float,
    expected_candles: int,
) -> dict[str, Any]:
    with_probs = [s for s in signals if s.probabilities]
    decisions = Counter(s.decision for s in signals)
    errors = Counter(_error_kind(s.reason) for s in signals if not s.probabilities)
    max_dir = [max(s.probabilities["UP"], s.probabilities["DOWN"]) for s in with_probs]  # type: ignore[index]

    # калибровка по накопленным результатам
    resolved = [s for s in with_probs if s.outcome_label]
    calibration: dict[str, Any] = {"resolved": len(resolved)}
    if resolved:
        labels = [Label(s.outcome_label) for s in resolved]  # type: ignore[arg-type]
        probs = [[float(s.probabilities[c.value]) for c in CLASSES] for s in resolved]  # type: ignore[index]
        prior = (
            model.artifact.get("train_class_distribution")
            or model.artifact["metrics"]["validation"]["class_distribution"]
        )
        prior_probs = [[float(prior[c.value]) for c in CLASSES]] * len(resolved)
        calibration.update(
            log_loss=log_loss(labels, probs),
            baseline_log_loss=log_loss(labels, prior_probs),
            brier=brier(labels, probs),
            baseline_brier=brier(labels, prior_probs),
            ece=_ece(labels, probs),
            enough=len(resolved) >= MIN_OUTCOMES_FOR_METRICS,
            outcome_distribution={
                c.value: sum(1 for y in labels if y is c) / len(labels) for c in CLASSES
            },
        )

    # направленные сигналы (вход без позиции) и их итог за горизонт
    entries = [s for s in signals if s.decision in ("BUY", "SELL") and s.outcome_return is not None]
    net = [
        (s.outcome_return if s.decision == "BUY" else -s.outcome_return) - cost  # type: ignore[operator]
        for s in entries
    ]
    hits = sum(
        1
        for s in entries
        if (s.decision == "BUY" and s.outcome_label == "UP")
        or (s.decision == "SELL" and s.outcome_label == "DOWN")
    )

    latency = [(s.created_at - s.decision_time).total_seconds() for s in signals]
    spreads = [s.spread_bps for s in signals if s.spread_bps is not None]
    slippage = [
        s.entry_slippage_bps
        for s in signals
        if s.entry_slippage_bps is not None and s.decision in ("BUY", "SELL")
    ]
    refused = [s for s in signals if s.decision in ("BUY", "SELL") and s.risk_reasons]
    refusal_reasons = Counter(r.split(":")[0] for s in refused for r in (s.risk_reasons or []))
    executed = Counter(s.execution_status for s in signals if s.execution_status)

    return {
        "version": model.version,
        "status": model.status,
        "signals": len(signals),
        "coverage": len({s.decision_time for s in signals}) / expected_candles
        if expected_candles
        else None,
        "decisions": dict(decisions),
        "feature_or_model_errors": dict(errors),
        "probabilities": {
            "mean": {
                c.value: fmean(float(s.probabilities[c.value]) for s in with_probs)  # type: ignore[index]
                for c in CLASSES
            }
            if with_probs
            else None,
            "max_directional_p50": _quantile(max_dir, 0.5),
            "max_directional_p90": _quantile(max_dir, 0.9),
        },
        "calibration": calibration,
        "entries": {
            "resolved": len(entries),
            "precision": hits / len(entries) if entries else None,
            "mean_net_return": fmean(net) if net else None,
            "sum_net_return": math.fsum(net),
        },
        "latency_s": {
            "p50": _quantile(latency, 0.5),
            "p95": _quantile(latency, 0.95),
            "max": max(latency) if latency else None,
        },
        "market": {
            "spread_bps_mean": fmean(spreads) if spreads else None,
            "spread_bps_max": max(spreads) if spreads else None,
            "entry_slippage_bps_mean": fmean(slippage) if slippage else None,
        },
        "risk_refusals": {"count": len(refused), "reasons": dict(refusal_reasons)},
        "execution": dict(executed),
    }


def _drift(
    model: ModelVersionRecord, snapshots: Sequence[FeatureSnapshotRecord]
) -> list[dict[str, Any]]:
    stats: dict[str, dict[str, float]] = model.artifact.get("feature_stats") or {}
    bounds: dict[str, list[float]] = model.artifact.get("feature_bounds") or {}
    if not snapshots:
        return []
    rows = []
    for name in model.artifact.get("feature_names", []):
        values = [float(s.values[name]) for s in snapshots if name in s.values]
        if not values:
            continue
        entry: dict[str, Any] = {"feature": name, "live_mean": fmean(values)}
        st = stats.get(name)
        if st and st["std"] > 0:
            entry["shift_sigma"] = (entry["live_mean"] - st["mean"]) / st["std"]
            entry["outside_p01_p99"] = sum(
                1 for v in values if not st["p01"] <= v <= st["p99"]
            ) / len(values)
        b = bounds.get(name)
        if b:
            entry["out_of_bounds"] = sum(1 for v in values if not b[0] <= v <= b[1]) / len(values)
        rows.append(entry)
    rows.sort(key=lambda r: -abs(r.get("shift_sigma", 0.0)))
    return rows


async def shadow_report(
    session: AsyncSession,
    market_id: int,
    *,
    since: datetime,
    now: datetime,
    step: timedelta,
    cost_bps: float,
) -> dict[str, Any]:
    signals = list(
        (
            await session.execute(
                select(SignalRecord)
                .where(SignalRecord.market_id == market_id, SignalRecord.decision_time >= since)
                .order_by(SignalRecord.decision_time)
            )
        ).scalars()
    )
    snapshots = list(
        (
            await session.execute(
                select(FeatureSnapshotRecord).where(
                    FeatureSnapshotRecord.market_id == market_id,
                    FeatureSnapshotRecord.decision_time >= since,
                )
            )
        ).scalars()
    )
    versions = sorted({s.model_version for s in signals})
    models = {
        m.version: m
        for m in (
            await session.execute(
                select(ModelVersionRecord).where(ModelVersionRecord.version.in_(versions))
            )
        ).scalars()
    }
    first = signals[0].decision_time if signals else None
    sections = []
    for version in versions:
        model_signals = [s for s in signals if s.model_version == version]
        start = model_signals[0].decision_time
        expected = int((now - start) // step) + 1
        section = _model_section(
            models[version], model_signals, cost=cost_bps / 10_000, expected_candles=expected
        )
        section["drift"] = _drift(models[version], snapshots)
        sections.append(section)
    return {
        "since": since.isoformat(),
        "first_signal": first.isoformat() if first else None,
        "now": now.isoformat(),
        "snapshots": len(snapshots),
        "min_outcomes_for_metrics": MIN_OUTCOMES_FOR_METRICS,
        "drift_sigma_warn": DRIFT_SIGMA_WARN,
        "models": sections,
    }
