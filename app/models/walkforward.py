"""Скользящая проверка (walk-forward) вариантов метки на годах истории.

История: Binance USDⓈ-M BTCUSDT (до начала данных Hyperliquid) + Hyperliquid (дальше) —
цена на них движется одинаково (сверка: метки совпадают на 99%). Для каждого проверочного
окна (по умолчанию квартал) модель учится только на прошлом:

    [ train ……………… ] purge [ validation 60 д ] purge [ calibration 30 д ] purge [ test ]

validation — ранняя остановка и подбор порога уверенности под желаемую частоту сделок,
calibration — температура, test — оценка. Результаты всех окон складываются: сотни сделок
вместо десятков, со статистической значимостью и сравнением со случайными входами.
Признаки, расходящиеся между биржами (funding, premium, размер сделки), не используются.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import fmean, pstdev
from typing import Any

from catboost import CatBoostClassifier

from app.domain.market import Candle, FundingPoint
from app.features.dataset import DatasetRow, compare_sources, feature_rows
from app.features.pipeline import FEATURE_NAMES
from app.features.target import CLASSES, Label, Outcome, TargetSpec
from app.models.calibration import fit_temperature, softmax
from app.models.experiment import (
    THETAS,
    ExpRow,
    Trade,
    calibrate_k,
    choose_theta,
    outcome_for,
    select_trades,
    trade_stats,
)
from app.models.training import CATBOOST_PARAMS

HOUR = timedelta(hours=1)
# быстрее, чем в боевом обучении (окон много); на выводы о метке это не влияет
WALK_FORWARD_CATBOOST: dict[str, Any] = {"learning_rate": 0.06, "od_wait": 100}


@dataclass(frozen=True, slots=True)
class Fold:
    train_end: datetime
    val: tuple[datetime, datetime]
    cal: tuple[datetime, datetime]
    test: tuple[datetime, datetime]


def _quarter_start(t: datetime) -> datetime:
    return datetime(t.year, 3 * ((t.month - 1) // 3) + 1, 1, tzinfo=UTC)


def _add_months(t: datetime, months: int) -> datetime:
    m = t.month - 1 + months
    return t.replace(year=t.year + m // 12, month=m % 12 + 1, day=1)


def make_folds(
    first: datetime,
    last: datetime,
    *,
    purge: timedelta,
    min_train_months: int = 18,
    test_months: int = 3,
    val_days: int = 60,
    cal_days: int = 30,
) -> list[Fold]:
    folds = []
    start = _quarter_start(_add_months(first, min_train_months))
    while start < last:
        end = min(_add_months(start, test_months), last + HOUR)
        cal_end = start - purge
        cal_start = cal_end - timedelta(days=cal_days)
        val_end = cal_start - purge
        val_start = val_end - timedelta(days=val_days)
        folds.append(
            Fold(val_start - purge, (val_start, val_end), (cal_start, cal_end), (start, end))
        )
        start = end
    return folds


def _within(rows: Sequence[ExpRow], span: tuple[datetime, datetime]) -> list[ExpRow]:
    return [r for r in rows if span[0] <= r.decision_time < span[1]]


def _t_stat(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    sd = pstdev(values)
    return fmean(values) / (sd / math.sqrt(len(values))) if sd > 0 else None


def summarize(trades: Sequence[Trade], days: float) -> dict[str, Any]:
    stats = trade_stats(trades, days)
    nets = [t.net for t in trades]
    stats["t_stat"] = _t_stat(nets)
    return stats


def walk_forward(
    primary: tuple[Sequence[Candle], Sequence[FundingPoint]],
    auxiliary: tuple[Sequence[Candle], Sequence[FundingPoint]] | None,
    variants: Sequence[TargetSpec],
    *,
    cost_bps: Decimal,
    edge_bps: Decimal,
    trades_per_day: float,
    min_train_months: int = 18,
    test_months: int = 3,
    catboost_params: dict[str, Any] | None = None,
    progress: Any = None,
) -> dict[str, Any]:
    max_h = max(v.horizon for v in variants)
    purge = max_h * HOUR
    candles: dict[str, list[Candle]] = {"hyperliquid": [c for c in primary[0] if c.is_closed]}
    hl, _ = feature_rows(candles["hyperliquid"], primary[1], horizon=max_h)
    rows = [ExpRow(r.decision_time, r.features, r.index, "hyperliquid", "") for r in hl]
    ignored: list[str] = []
    if auxiliary is not None:
        candles["binance"] = [c for c in auxiliary[0] if c.is_closed]
        aux, _ = feature_rows(candles["binance"], auxiliary[1], horizon=max_h)
        overlap = compare_sources(
            [DatasetRow(r.decision_time, r.features, 0.0, Label.FLAT) for r in hl],
            [DatasetRow(r.decision_time, r.features, 0.0, Label.FLAT) for r in aux],
        )
        ignored = sorted(overlap.get("weak_features", []))
        first_hl = hl[0].decision_time if hl else datetime.max.replace(tzinfo=UTC)
        rows = [
            ExpRow(r.decision_time, r.features, r.index, "binance", "")
            for r in aux
            if r.decision_time < first_hl
        ] + rows
    rows.sort(key=lambda r: r.decision_time)
    if not rows:
        raise ValueError("нет строк для проверки")

    folds = make_folds(
        rows[0].decision_time,
        rows[-1].decision_time,
        purge=purge,
        min_train_months=min_train_months,
        test_months=test_months,
    )
    cost = float(cost_bps) / 10_000
    edge = float(edge_bps) / 10_000
    cb_params = {**CATBOOST_PARAMS, **WALK_FORWARD_CATBOOST, **(catboost_params or {})}
    if ignored:
        cb_params["ignored_features"] = [FEATURE_NAMES.index(n) for n in ignored]

    results = []
    for base in variants:
        spec = base
        if spec.kind == "vol":  # k — по данным до первого проверочного окна (без заглядывания)
            spec = replace(
                spec,
                k=calibrate_k(
                    spec, [r for r in rows if r.decision_time < folds[0].train_end], candles
                ),
            )
        outcomes: dict[datetime, Outcome] = {
            r.decision_time: outcome_for(spec, candles[r.source], r) for r in rows
        }
        all_trades: list[Trade] = []
        fold_reports = []
        random_long: list[float] = []
        test_days = 0.0
        days_by_year: dict[int, float] = defaultdict(float)
        for n, fold in enumerate(folds, 1):
            train = [r for r in rows if r.decision_time < fold.train_end]
            val, cal, test = (_within(rows, s) for s in (fold.val, fold.cal, fold.test))
            if not (train and val and cal and test):
                continue
            if progress:
                progress(f"{spec.name}: окно {n}/{len(folds)} ({fold.test[0]:%Y-%m})")
            model = CatBoostClassifier(**cb_params, class_names=[c.value for c in CLASSES])
            model.fit(
                [list(r.features) for r in train],
                [outcomes[r.decision_time].label.value for r in train],
                eval_set=(
                    [list(r.features) for r in val],
                    [outcomes[r.decision_time].label.value for r in val],
                ),
            )

            def raw(part: Sequence[ExpRow], m: CatBoostClassifier = model) -> list[list[float]]:
                out = m.predict(
                    [list(r.features) for r in part],
                    prediction_type="RawFormulaVal",
                    thread_count=1,
                )
                return [[float(x) for x in row] for row in out]

            temperature = fit_temperature(raw(cal), [outcomes[r.decision_time].label for r in cal])
            train_out = [outcomes[r.decision_time] for r in train]
            mean_long = {
                c: fmean([o.long_return for o in train_out if o.label is c] or [0.0])
                for c in CLASSES
            }
            mean_short = {
                c: fmean([o.short_return for o in train_out if o.label is c] or [0.0])
                for c in CLASSES
            }
            probs_val = [list(softmax(x, temperature)) for x in raw(val)]
            days_val = max(
                (val[-1].decision_time - val[0].decision_time).total_seconds() / 86_400, 1e-9
            )
            curve = []
            for theta in THETAS:
                trades = select_trades(
                    val,
                    [outcomes[r.decision_time] for r in val],
                    probs_val,
                    theta=theta,
                    mean_long=mean_long,
                    mean_short=mean_short,
                    edge=edge,
                    cost=cost,
                    horizon=spec.horizon,
                )
                curve.append({"theta": theta, **trade_stats(trades, days_val)})
            chosen = choose_theta(curve, trades_per_day)
            days = (fold.test[1] - fold.test[0]).total_seconds() / 86_400
            test_days += days
            days_by_year[fold.test[0].year] += days
            random_long.extend(outcomes[r.decision_time].long_return for r in test)
            trades = []
            if chosen is not None:
                trades = select_trades(
                    test,
                    [outcomes[r.decision_time] for r in test],
                    [list(softmax(x, temperature)) for x in raw(test)],
                    theta=chosen["theta"],
                    mean_long=mean_long,
                    mean_short=mean_short,
                    edge=edge,
                    cost=cost,
                    horizon=spec.horizon,
                )
            all_trades.extend(trades)
            fold_reports.append(
                {
                    "test_start": fold.test[0].isoformat(),
                    "test_end": fold.test[1].isoformat(),
                    "source": Counter(r.source for r in test).most_common(1)[0][0],
                    "train_rows": len(train),
                    "theta": chosen["theta"] if chosen else None,
                    **summarize(trades, days),
                }
            )
        by_year: dict[int, list[Trade]] = defaultdict(list)
        for t in all_trades:
            by_year[t.time.year].append(t)
        drift = fmean(random_long) if random_long else None
        results.append(
            {
                "variant": spec.name,
                "kind": spec.kind,
                "horizon": spec.horizon,
                "k": spec.k if spec.kind == "vol" else None,
                "total": summarize(all_trades, test_days),
                "folds": fold_reports,
                "profitable_folds": sum(1 for f in fold_reports if f["sum_net"] > 0),
                "folds_with_trades": sum(1 for f in fold_reports if f["trades"]),
                "by_year": {
                    str(y): summarize(ts, days_by_year[y]) for y, ts in sorted(by_year.items())
                },
                "random_entry": {
                    "long_mean_net": drift - cost if drift is not None else None,
                    "short_mean_net": -drift - cost if drift is not None else None,
                },
            }
        )
    return {
        "folds": len(folds),
        "first_test": folds[0].test[0].isoformat() if folds else None,
        "last_test_end": folds[-1].test[1].isoformat() if folds else None,
        "rows_by_source": dict(Counter(r.source for r in rows)),
        "ignored_features": ignored,
        "cost_bps": float(cost_bps),
        "trades_per_day_target": trades_per_day,
        "catboost": {k: v for k, v in cb_params.items() if k != "verbose"},
        "note": (
            "Оценка сигналов без наложения позиций, вход и выход по close/барьеру; без "
            "проскальзывания сверх заложенных издержек. Модель каждого окна видит только прошлое."
        ),
        "variants": results,
    }
