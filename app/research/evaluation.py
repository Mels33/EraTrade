"""Общие инструменты честной проверки: скользящие окна, статистика сделок, классы меток.

Окно проверки: [ train ] purge [ validation ] purge [ calibration ] purge [ test ] —
модель каждого окна видит только прошлое; test используется только для оценки.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from statistics import fmean, pstdev
from typing import Any

HOUR = timedelta(hours=1)


class Label(StrEnum):
    DOWN = "DOWN"
    FLAT = "FLAT"
    UP = "UP"


# Фиксированный порядок классов: индексы вероятностей модели.
CLASSES: tuple[Label, ...] = (Label.DOWN, Label.FLAT, Label.UP)


@dataclass(frozen=True, slots=True)
class Trade:
    time: datetime
    side: str
    net: float


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


def t_stat(values: Sequence[float]) -> float | None:
    if len(values) < 3:
        return None
    sd = pstdev(values)
    return fmean(values) / (sd / math.sqrt(len(values))) if sd > 0 else None


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


def summarize(trades: Sequence[Trade], days: float) -> dict[str, Any]:
    stats = trade_stats(trades, days)
    stats["t_stat"] = t_stat([t.net for t in trades])
    return stats


def choose_theta(curve: Sequence[dict[str, Any]], trades_per_day: float) -> dict[str, Any] | None:
    """θ с частотой ближе всего к желаемой (при равенстве — выше, т.е. осторожнее)."""
    candidates = [c for c in curve if c["trades"] > 0]
    if not candidates:
        return None
    return min(candidates, key=lambda c: (abs(c["per_day"] - trades_per_day), -c["theta"]))
