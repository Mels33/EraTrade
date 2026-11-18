"""Скользящая проверка модели «спрос и предложение» на минутных данных Binance.

Две модели:
- flow — только поток агрессивных сделок + контекст цены (история с 2023, квартальные окна,
  обучение — последние 12 месяцев);
- book — поток + ближний стакан ±0.2% (уровень есть в архиве с января 2026: месячные окна,
  обучение — вся история с января).
Решение каждую минуту; метки на горизонтах 1/5/15 мин; исполнение taker и maker
(app/models/execution_model.py) по минутным барам; порог уверенности подбирается на
validation под каждую целевую частоту сделок; test — только оценка.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from statistics import fmean
from typing import Any, Literal, NamedTuple

import numpy as np
import pandas as pd
from catboost import CatBoostClassifier

from app.features.target import CLASSES
from app.models.calibration import fit_temperature, softmax
from app.models.execution_model import Costs, simulate
from app.models.experiment import Trade, choose_theta
from app.models.training import CATBOOST_PARAMS
from app.models.walkforward import make_folds, summarize
from app.research.features_micro import compute, feature_set

MINUTE = timedelta(minutes=1)
# gross — диагностика без издержек (вход по close, без комиссий и условия исполнения):
# есть ли у признаков вообще сила предсказания направления и насколько она меньше издержек.
COST_MODELS: tuple[str, ...] = ("gross", "taker", "maker")
# порог ожидаемой выгоды E[r] = издержки исполнения (без запаса — исследование)
EDGE: dict[str, float] = {"gross": 0.0, "taker": 0.0014, "maker": 0.0006}
ZERO_COSTS = Costs(maker_fee=0.0, taker_fee=0.0, half_spread=0.0, slippage=0.0)
THETAS = [round(0.34 + 0.02 * i, 2) for i in range(24)]  # 0.34 … 0.80
FAST_CATBOOST: dict[str, Any] = {
    "learning_rate": 0.08,
    "iterations": 1200,
    "od_wait": 60,
    "thread_count": 8,
}


@dataclass(frozen=True, slots=True)
class MicroSpec:
    name: str
    kind: Literal["ret", "barrier"]
    horizon: int  # минут
    k: float  # множитель σ·√h
    floor: float  # минимальный порог / дистанция барьера


VARIANTS: dict[str, MicroSpec] = {
    "ret1": MicroSpec("ret1", "ret", 1, 0.5, 0.0005),
    "ret5": MicroSpec("ret5", "ret", 5, 0.5, 0.0005),
    "ret15": MicroSpec("ret15", "ret", 15, 0.5, 0.0005),
    "barrier5": MicroSpec("barrier5", "barrier", 5, 1.0, 0.0010),
    "barrier15": MicroSpec("barrier15", "barrier", 15, 1.0, 0.0010),
}


class Bar(NamedTuple):
    high: float
    low: float
    close: float


class BarView:
    """Доступ к минутным барам по индексу без создания миллиона объектов."""

    def __init__(self, high: np.ndarray, low: np.ndarray, close: np.ndarray) -> None:
        self.h, self.l, self.c = high, low, close

    def __getitem__(self, j: int) -> Bar:
        return Bar(float(self.h[j]), float(self.l[j]), float(self.c[j]))

    def __len__(self) -> int:
        return len(self.c)


@dataclass(frozen=True, slots=True)
class Labels:
    label: np.ndarray  # 0 DOWN, 1 FLAT, 2 UP; -1 — нет данных
    long_ret: np.ndarray
    short_ret: np.ndarray
    distance: np.ndarray  # дистанция стопа/тейка для исполнения


def make_labels(spec: MicroSpec, bars: pd.DataFrame, sigma: np.ndarray) -> Labels:
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    n, h = len(close), spec.horizon
    minutes = bars["minute"].to_numpy("datetime64[ns]")
    ok = np.zeros(n, dtype=bool)
    ok[: n - h] = (minutes[h:] - minutes[: n - h]) == np.timedelta64(60 * h, "s")
    ok &= np.isfinite(sigma) & np.isfinite(close)
    # все бары горизонта должны иметь цены (минуты без сделок — нет цены, строку не размечаем)
    finite = np.isfinite(close) & np.isfinite(high) & np.isfinite(low)
    for j in range(1, h + 1):
        ok[: n - j] &= finite[j:]
    thr = np.maximum(spec.floor, spec.k * np.nan_to_num(sigma) * math.sqrt(h))
    label = np.full(n, -1, dtype=int)
    long_ret = np.full(n, np.nan)
    if spec.kind == "ret":
        r = np.full(n, np.nan)
        r[: n - h] = close[h:] / close[: n - h] - 1
        label[ok] = 1
        label[ok & (r > thr)] = 2
        label[ok & (r < -thr)] = 0
        long_ret[ok] = r[ok]
        return Labels(label, long_ret, -long_ret, thr)
    # barrier: что раньше за h минут — +D или −D; оба в одной минуте → FLAT, обе стороны — стоп
    decided = np.zeros(n, dtype=bool)
    short_ret = np.full(n, np.nan)
    for j in range(1, h + 1):
        idx = np.arange(n - j)
        up = np.zeros(n, dtype=bool)
        dn = np.zeros(n, dtype=bool)
        up[idx] = high[idx + j] >= close[idx] * (1 + thr[idx])
        dn[idx] = low[idx + j] <= close[idx] * (1 - thr[idx])
        new = ok & ~decided
        both, only_up, only_dn = new & up & dn, new & up & ~dn, new & dn & ~up
        label[both] = 1
        long_ret[both], short_ret[both] = -thr[both], -thr[both]
        label[only_up] = 2
        long_ret[only_up], short_ret[only_up] = thr[only_up], -thr[only_up]
        label[only_dn] = 0
        long_ret[only_dn], short_ret[only_dn] = -thr[only_dn], thr[only_dn]
        decided |= both | only_up | only_dn
    rest = ok & ~decided
    r = np.full(n, np.nan)
    r[: n - h] = close[h:] / close[: n - h] - 1
    label[rest] = 1
    long_ret[rest], short_ret[rest] = r[rest], -r[rest]
    return Labels(label, long_ret, short_ret, thr)


def select(
    idx: np.ndarray,
    times: np.ndarray,
    probs: np.ndarray,
    *,
    theta: float,
    mean_long: np.ndarray,
    mean_short: np.ndarray,
    edge: float,
    horizon: int,
    value: Callable[[int, Literal["long", "short"]], float | None],
) -> tuple[list[Trade], int]:
    """Сделки без наложения; неисполненная maker-заявка позицию не занимает."""
    up, down = probs[:, 2], probs[:, 0]
    e_long = probs @ mean_long
    e_short = probs @ mean_short
    want_long = (up >= theta) & (up > down) & (e_long > edge)
    want_short = (down >= theta) & (down > up) & (e_short > edge) & ~want_long
    candidates = np.where(want_long | want_short)[0]
    trades: list[Trade] = []
    attempts = 0
    busy_until = np.datetime64("NaT", "ns")
    for k in candidates:
        t = times[k]
        if not np.isnat(busy_until) and t < busy_until:
            continue
        side: Literal["long", "short"] = "long" if want_long[k] else "short"
        attempts += 1
        net = value(int(idx[k]), side)
        if net is None or not math.isfinite(net):
            continue
        trades.append(Trade(pd.Timestamp(t, tz="UTC").to_pydatetime(), side, net))
        busy_until = t + np.timedelta64(60 * (horizon + 1), "s")
    return trades, attempts


def _days(times: np.ndarray) -> float:
    return max(float((times[-1] - times[0]) / np.timedelta64(1, "D")), 1e-9)


def walk_forward_micro(
    bars: pd.DataFrame,
    variants: Sequence[MicroSpec],
    *,
    depth: bool,
    trades_per_day: Sequence[float] = (1.0, 5.0, 20.0),
    test_months: int,
    min_train_months: int,
    max_train_months: int | None,
    val_days: int = 14,
    cal_days: int = 7,
    train_stride: int = 5,
    deep: bool = False,
    catboost_params: dict[str, Any] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    names = feature_set(depth=depth, deep=deep)
    x, valid = compute(bars, depth=depth, deep=deep)
    times = bars["minute"].to_numpy("datetime64[ns]") + np.timedelta64(60, "s")  # конец минуты
    sigma = x[:, names.index("rv_60")]
    rows = np.where(valid)[0]
    if len(rows) == 0:
        raise ValueError("нет валидных строк")
    first = pd.Timestamp(times[rows[0]], tz="UTC").to_pydatetime()
    last = pd.Timestamp(times[rows[-1]], tz="UTC").to_pydatetime()
    max_h = max(v.horizon for v in variants)
    folds = make_folds(
        first,
        last,
        purge=(max_h + 1) * MINUTE,
        min_train_months=min_train_months,
        test_months=test_months,
        val_days=val_days,
        cal_days=cal_days,
    )
    view = BarView(
        bars["high"].to_numpy(float), bars["low"].to_numpy(float), bars["close"].to_numpy(float)
    )
    params = {**CATBOOST_PARAMS, **FAST_CATBOOST, **(catboost_params or {})}
    row_times = times[rows]

    def span(a: datetime, b: datetime) -> np.ndarray:
        lo = np.searchsorted(row_times, np.datetime64(a.replace(tzinfo=None), "ns"))
        hi = np.searchsorted(row_times, np.datetime64(b.replace(tzinfo=None), "ns"))
        out: np.ndarray = rows[lo:hi]
        return out

    results = []
    for spec in variants:
        lab = make_labels(spec, bars, sigma)
        acc: dict[tuple[str, float], dict[str, Any]] = {
            (cm, f): {"trades": [], "attempts": 0, "folds": []}
            for cm in COST_MODELS
            for f in trades_per_day
        }
        random_entry: dict[str, dict[str, list[float]]] = {
            cm: {"long": [], "short": []} for cm in COST_MODELS
        }
        test_days = 0.0
        for n, fold in enumerate(folds, 1):
            train_start = (
                fold.train_end - timedelta(days=30 * max_train_months)
                if max_train_months
                else first
            )
            parts = {
                "train": span(train_start, fold.train_end)[::train_stride],
                "val": span(*fold.val),
                "cal": span(*fold.cal),
                "test": span(*fold.test),
            }
            parts = {k: v[lab.label[v] >= 0] for k, v in parts.items()}
            if any(len(v) == 0 for v in parts.values()):
                continue
            if progress:
                progress(f"{spec.name}: окно {n}/{len(folds)} ({fold.test[0]:%Y-%m})")
            classes = np.array([c.value for c in CLASSES])
            model = CatBoostClassifier(**params, class_names=list(classes))
            model.fit(
                x[parts["train"]],
                classes[lab.label[parts["train"]]],
                eval_set=(x[parts["val"]], classes[lab.label[parts["val"]]]),
            )

            def raw(ix: np.ndarray, m: CatBoostClassifier = model) -> np.ndarray:
                out: np.ndarray = np.asarray(
                    m.predict(x[ix], prediction_type="RawFormulaVal"), dtype=float
                )
                return out

            temp = fit_temperature(
                raw(parts["cal"]).tolist(), [CLASSES[i] for i in lab.label[parts["cal"]]]
            )
            tr = parts["train"]
            mean_long = np.array(
                [
                    np.nanmean(lab.long_ret[tr][lab.label[tr] == c])
                    if (lab.label[tr] == c).any()
                    else 0.0
                    for c in range(3)
                ]
            )
            mean_short = np.array(
                [
                    np.nanmean(lab.short_ret[tr][lab.label[tr] == c])
                    if (lab.label[tr] == c).any()
                    else 0.0
                    for c in range(3)
                ]
            )
            probs = {
                k: np.array([softmax(r, temp) for r in raw(parts[k])]) for k in ("val", "test")
            }
            days_val = _days(times[parts["val"]])
            days_test = (fold.test[1] - fold.test[0]).total_seconds() / 86_400
            test_days += days_test

            for cm in COST_MODELS:

                def value(
                    i: int,
                    side: Literal["long", "short"],
                    cm: str = cm,
                    spec: MicroSpec = spec,
                    lab: Labels = lab,
                ) -> float | None:
                    return simulate(
                        view,  # type: ignore[arg-type]
                        i,
                        side=side,
                        horizon=spec.horizon,
                        distance=float(lab.distance[i]),
                        take_profit=spec.kind == "barrier",
                        model="taker" if cm == "gross" else cm,  # type: ignore[arg-type]
                        costs=ZERO_COSTS if cm == "gross" else Costs(),
                    )

                curve = []
                for theta in THETAS:
                    trades, _ = select(
                        parts["val"],
                        times[parts["val"]],
                        probs["val"],
                        theta=theta,
                        mean_long=mean_long,
                        mean_short=mean_short,
                        edge=EDGE[cm],
                        horizon=spec.horizon,
                        value=value,
                    )
                    curve.append({"theta": theta, **summarize(trades, days_val)})
                for target in trades_per_day:
                    chosen = choose_theta(curve, target)
                    trades, attempts = (
                        select(
                            parts["test"],
                            times[parts["test"]],
                            probs["test"],
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
                    a = acc[(cm, target)]
                    a["trades"].extend(trades)
                    a["attempts"] += attempts
                    a["folds"].append(
                        {
                            "test_start": fold.test[0].isoformat(),
                            "theta": chosen["theta"] if chosen else None,
                            **summarize(trades, days_test),
                        }
                    )
                for side in ("long", "short"):
                    sample = parts["test"][:: max(spec.horizon, 5)]
                    random_entry[cm][side].extend(
                        v for i in sample if (v := value(int(i), side)) is not None
                    )

        per = []
        for (cm, target), a in acc.items():
            trades = a["trades"]
            per.append(
                {
                    "cost_model": cm,
                    "trades_per_day_target": target,
                    "total": summarize(trades, test_days),
                    "long": summarize([t for t in trades if t.side == "long"], test_days),
                    "short": summarize([t for t in trades if t.side == "short"], test_days),
                    "fill_rate": len(trades) / a["attempts"] if a["attempts"] else None,
                    "profitable_folds": sum(1 for f in a["folds"] if f["sum_net"] > 0),
                    "folds": a["folds"],
                }
            )
        results.append(
            {
                "variant": spec.name,
                "horizon_min": spec.horizon,
                "kind": spec.kind,
                "class_share": {
                    c.value: float((lab.label[rows] == i).mean()) for i, c in enumerate(CLASSES)
                },
                "random_entry": {
                    cm: {s: (fmean(v) if v else None) for s, v in sides.items()}
                    for cm, sides in random_entry.items()
                },
                "results": per,
            }
        )
    return {
        "model": "book" if depth else "flow",
        "deep": deep,
        "features": list(names),
        "rows_valid": len(rows),
        "first": first.isoformat(),
        "last": last.isoformat(),
        "folds": len(folds),
        "first_test": folds[0].test[0].isoformat() if folds else None,
        "costs": asdict(Costs()),
        "edge": EDGE,
        "note": (
            "Оценка по минутным барам: maker-вход засчитан, только если цена прошла сквозь "
            "лимит в следующей минуте; в одной минуте задеты оба барьера — стоп. Одна позиция "
            "за раз. Перед любым живым контуром — точная проверка на тиках."
        ),
        "variants": results,
    }
