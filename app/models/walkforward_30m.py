"""Скользящая проверка 30-минутной стратегии: признаки v2 (с потоком сделок), варианты
метки, исполнение taker и maker, разбивка long / short.

Данные — Binance USDⓈ-M BTCUSDT 30m с taker buy volume (история с 2020): у Hyperliquid
истории потока сделок нет, она копится коллектором. Цена на биржах движется одинаково
(сверка на 1h: метки совпадают на 99%). Окна, как в walkforward.py: модель каждого
квартала учится только на прошлом, порог уверенности подбирается на validation.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from statistics import fmean
from typing import Any, Literal

from catboost import CatBoostClassifier

from app.domain.market import Candle
from app.features.pipeline import FeatureError
from app.features.pipeline_v2 import (
    FEATURE_NAMES,
    LOOKBACK,
    STEP,
    FlowBar,
    binance_buy_ratio,
    compute_v2,
)
from app.features.target import CLASSES, Label, Outcome, TargetSpec, label_barrier, label_vol
from app.models.calibration import fit_temperature, softmax
from app.models.execution_model import CostModel, Costs, simulate
from app.models.experiment import THETAS, Trade, choose_theta
from app.models.training import CATBOOST_PARAMS
from app.models.walkforward import make_folds, summarize

RV = FEATURE_NAMES.index("rv_48")
ATR = FEATURE_NAMES.index("atr_14_rel")
VOL_FLAT_SHARE = 0.4
COST_MODELS: tuple[CostModel, ...] = ("taker", "maker")
# порог ожидаемой выгоды E[r] = издержки модели исполнения + запас 5 bp
EDGE: dict[CostModel, float] = {"taker": 0.0019, "maker": 0.0010}
FAST_CATBOOST: dict[str, Any] = {
    "learning_rate": 0.08,
    "iterations": 1500,
    "od_wait": 80,
    "thread_count": 8,
}

VARIANTS_30M: dict[str, TargetSpec] = {
    "vol4": TargetSpec("vol4", "vol", 4),
    "vol8": TargetSpec("vol8", "vol", 8),
    "barrier8": TargetSpec("barrier8", "barrier", 8),
    "barrier16": TargetSpec("barrier16", "barrier", 16),
}


@dataclass(frozen=True, slots=True)
class Row:
    decision_time: datetime
    index: int
    features: tuple[float, ...]


# --- признаки параллельно ---

_BARS: list[FlowBar] = []


def _init(bars: list[FlowBar]) -> None:
    global _BARS
    _BARS = bars


def _features_chunk(args: tuple[list[int], int]) -> list[Row | None]:
    indices, horizon = args
    out: list[Row | None] = []
    for i in indices:
        cur, fut = _BARS[i].candle, _BARS[i + horizon].candle
        if fut.open_time - cur.open_time != horizon * STEP:
            out.append(None)
            continue
        try:
            v = compute_v2(_BARS[i - LOOKBACK + 1 : i + 1])
        except FeatureError:
            out.append(None)
            continue
        out.append(Row(v.decision_time, i, v.values))
    return out


def build_rows(candles: Sequence[Candle], *, horizon: int, workers: int = 8) -> list[Row]:
    bars = [FlowBar(c, binance_buy_ratio(c)) for c in candles if c.is_closed]
    indices = list(range(LOOKBACK - 1, len(bars) - horizon))
    size = max(1, len(indices) // (workers * 8))
    chunks = [(indices[k : k + size], horizon) for k in range(0, len(indices), size)]
    if workers <= 1:
        _init(bars)
        results = [_features_chunk(c) for c in chunks]
    else:
        with ProcessPoolExecutor(workers, initializer=_init, initargs=(bars,)) as pool:
            results = list(pool.map(_features_chunk, chunks))
    rows = [r for chunk in results for r in chunk if r is not None]
    return rows


# --- метки и исполнение ---


def distance(row: Row, spec: TargetSpec) -> float:
    return max(spec.atr_mult * row.features[ATR], spec.min_distance)


def label(spec: TargetSpec, bars: Sequence[Candle], row: Row) -> Outcome:
    if spec.kind == "vol":
        return label_vol(
            bars,
            row.index,
            spec.horizon,
            sigma_1h=row.features[RV],
            k=spec.k,
            floor_bps=spec.floor_bps,
        )
    return label_barrier(bars, row.index, spec.horizon, distance=distance(row, spec))


def calibrate_k(spec: TargetSpec, rows: Sequence[Row], bars: Sequence[Candle]) -> float:
    z = sorted(
        abs(float(bars[r.index + spec.horizon].close / bars[r.index].close - 1))
        / (r.features[RV] * math.sqrt(spec.horizon))
        for r in rows
        if r.features[RV] > 0
    )
    return z[int(VOL_FLAT_SHARE * (len(z) - 1))]


def trade_value(
    spec: TargetSpec,
    bars: Sequence[Candle],
    row: Row,
    side: Literal["long", "short"],
    model: CostModel,
) -> float | None:
    return simulate(
        bars,
        row.index,
        side=side,
        horizon=spec.horizon,
        distance=distance(row, spec),
        take_profit=spec.kind == "barrier",
        model=model,
        costs=Costs(),
    )


def select(
    rows: Sequence[Row],
    probs: Sequence[Sequence[float]],
    *,
    theta: float,
    mean_long: dict[Label, float],
    mean_short: dict[Label, float],
    edge: float,
    horizon: int,
    value: Callable[[Row, Literal["long", "short"]], float | None],
) -> tuple[list[Trade], int]:
    """Сделки без наложения. Неисполненная maker-заявка позицию не занимает.
    Возвращает (сделки, число попыток входа)."""
    trades: list[Trade] = []
    attempts = 0
    busy_until: datetime | None = None
    for row, p in zip(rows, probs, strict=True):
        if busy_until is not None and row.decision_time < busy_until:
            continue
        pr = dict(zip(CLASSES, p, strict=True))
        side: Literal["long", "short"] | None = None
        if (
            pr[Label.UP] >= theta
            and pr[Label.UP] > pr[Label.DOWN]
            and math.fsum(pr[c] * mean_long[c] for c in CLASSES) > edge
        ):
            side = "long"
        elif (
            pr[Label.DOWN] >= theta
            and pr[Label.DOWN] > pr[Label.UP]
            and math.fsum(pr[c] * mean_short[c] for c in CLASSES) > edge
        ):
            side = "short"
        if side is None:
            continue
        attempts += 1
        net = value(row, side)
        if net is None:
            continue  # заявка не исполнилась — свободны со следующей свечи
        trades.append(Trade(row.decision_time, side, net))
        busy_until = row.decision_time + (horizon + 1) * STEP
    return trades, attempts


def _within(rows: Sequence[Row], span: tuple[datetime, datetime]) -> list[Row]:
    return [r for r in rows if span[0] <= r.decision_time < span[1]]


def walk_forward_30m(
    candles: Sequence[Candle],
    variants: Sequence[TargetSpec],
    *,
    trades_per_day: float,
    min_train_months: int = 18,
    test_months: int = 3,
    val_days: int = 60,
    cal_days: int = 30,
    catboost_params: dict[str, Any] | None = None,
    workers: int = 8,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    bars = [c for c in candles if c.is_closed]
    max_h = max(v.horizon for v in variants)
    if progress:
        progress(f"признаки v2 для {len(bars)} свечей…")
    rows = build_rows(bars, horizon=max_h + 1, workers=workers)  # +1: свеча исполнения maker
    if not rows:
        raise ValueError("нет строк")
    purge = (max_h + 1) * STEP
    folds = make_folds(
        rows[0].decision_time,
        rows[-1].decision_time,
        purge=purge,
        min_train_months=min_train_months,
        test_months=test_months,
        val_days=val_days,
        cal_days=cal_days,
    )
    params = {**CATBOOST_PARAMS, **FAST_CATBOOST, **(catboost_params or {})}

    results = []
    for base in variants:
        spec = base
        if spec.kind == "vol":
            spec = replace(
                spec,
                k=calibrate_k(
                    spec, [r for r in rows if r.decision_time < folds[0].train_end], bars
                ),
            )
        outcomes = {r.decision_time: label(spec, bars, r) for r in rows}
        acc: dict[CostModel, dict[str, Any]] = {
            m: {"trades": [], "attempts": 0, "folds": [], "random": {"long": [], "short": []}}
            for m in COST_MODELS
        }
        test_days = 0.0
        days_by_year: dict[int, float] = defaultdict(float)
        for n, fold in enumerate(folds, 1):
            train = [r for r in rows if r.decision_time < fold.train_end]
            val, cal, test = (_within(rows, s) for s in (fold.val, fold.cal, fold.test))
            if not (train and val and cal and test):
                continue
            if progress:
                progress(f"{spec.name}: окно {n}/{len(folds)} ({fold.test[0]:%Y-%m})")
            model = CatBoostClassifier(**params, class_names=[c.value for c in CLASSES])
            model.fit(
                [list(r.features) for r in train],
                [outcomes[r.decision_time].label.value for r in train],
                eval_set=(
                    [list(r.features) for r in val],
                    [outcomes[r.decision_time].label.value for r in val],
                ),
            )

            def raw(part: Sequence[Row], m: CatBoostClassifier = model) -> list[list[float]]:
                out = m.predict([list(r.features) for r in part], prediction_type="RawFormulaVal")
                return [[float(x) for x in row] for row in out]

            temp = fit_temperature(raw(cal), [outcomes[r.decision_time].label for r in cal])
            tr_out = [outcomes[r.decision_time] for r in train]
            mean_long = {
                c: fmean([o.long_return for o in tr_out if o.label is c] or [0.0]) for c in CLASSES
            }
            mean_short = {
                c: fmean([o.short_return for o in tr_out if o.label is c] or [0.0]) for c in CLASSES
            }
            p_val = [list(softmax(x, temp)) for x in raw(val)]
            p_test = [list(softmax(x, temp)) for x in raw(test)]
            days_val = max(
                (val[-1].decision_time - val[0].decision_time).total_seconds() / 86_400, 1e-9
            )
            days = (fold.test[1] - fold.test[0]).total_seconds() / 86_400
            test_days += days
            days_by_year[fold.test[0].year] += days

            for cm in COST_MODELS:

                def value(
                    row: Row,
                    side: Literal["long", "short"],
                    cm: CostModel = cm,
                    spec: TargetSpec = spec,
                ) -> float | None:
                    return trade_value(spec, bars, row, side, cm)

                curve = []
                for theta in THETAS:
                    trades, _ = select(
                        val,
                        p_val,
                        theta=theta,
                        mean_long=mean_long,
                        mean_short=mean_short,
                        edge=EDGE[cm],
                        horizon=spec.horizon,
                        value=value,
                    )
                    curve.append({"theta": theta, **summarize(trades, days_val)})
                chosen = choose_theta(curve, trades_per_day)
                trades, attempts = (
                    select(
                        test,
                        p_test,
                        theta=chosen["theta"],
                        mean_long=mean_long,
                        mean_short=mean_short,
                        edge=EDGE[cm],
                        horizon=spec.horizon,
                        value=value,
                    )
                    if chosen
                    else ([], 0)
                )
                a = acc[cm]
                a["trades"].extend(trades)
                a["attempts"] += attempts
                a["folds"].append(
                    {
                        "test_start": fold.test[0].isoformat(),
                        "theta": chosen["theta"] if chosen else None,
                        **summarize(trades, days),
                    }
                )
                for side in ("long", "short"):
                    a["random"][side].extend(
                        x for r in test[:: spec.horizon] if (x := value(r, side)) is not None
                    )

        per_model = {}
        for cm, a in acc.items():
            trades = a["trades"]
            by_year: dict[int, list[Trade]] = defaultdict(list)
            for t in trades:
                by_year[t.time.year].append(t)
            per_model[cm] = {
                "total": summarize(trades, test_days),
                "long": summarize([t for t in trades if t.side == "long"], test_days),
                "short": summarize([t for t in trades if t.side == "short"], test_days),
                "fill_rate": len(trades) / a["attempts"] if a["attempts"] else None,
                "profitable_folds": sum(1 for f in a["folds"] if f["sum_net"] > 0),
                "folds": a["folds"],
                "by_year": {
                    str(y): summarize(ts, days_by_year[y]) for y, ts in sorted(by_year.items())
                },
                "random_entry": {
                    side: (fmean(v) if v else None) for side, v in a["random"].items()
                },
            }
        results.append(
            {
                "variant": spec.name,
                "kind": spec.kind,
                "horizon": spec.horizon,
                "k": spec.k if spec.kind == "vol" else None,
                "class_distribution": dict(Counter(o.label.value for o in outcomes.values())),
                "models": per_model,
            }
        )
    return {
        "interval": "30m",
        "feature_set": "v2-30m",
        "rows": len(rows),
        "first_row": rows[0].decision_time.isoformat(),
        "folds": len(folds),
        "first_test": folds[0].test[0].isoformat() if folds else None,
        "trades_per_day_target": trades_per_day,
        "costs": asdict(Costs()),
        "edge": EDGE,
        "note": (
            "Оценка сигналов без наложения позиций; maker-вход засчитан, только если цена "
            "прошла сквозь лимит в следующей свече; тейк-профит — тоже только при проходе "
            "сквозь уровень; стоп и выход по времени — по рынку с проскальзыванием."
        ),
        "variants": results,
    }
