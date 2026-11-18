"""Калибровка вероятностей температурой (temperature scaling).

Один параметр T: p = softmax(raw / T), где raw — сырые значения формулы CatBoost (логиты).
T подбирается минимизацией log loss на отдельном участке calibration. Один параметр
устойчив на небольшом участке и не меняет порядок классов внутри строки.
"""

import math
from collections.abc import Sequence

from app.research.evaluation import CLASSES, Label

T_MIN, T_MAX = 0.05, 20.0
GOLDEN = (math.sqrt(5) - 1) / 2


def softmax(raw: Sequence[float], temperature: float = 1.0) -> tuple[float, ...]:
    scaled = [x / temperature for x in raw]
    top = max(scaled)
    exps = [math.exp(x - top) for x in scaled]
    total = math.fsum(exps)
    return tuple(e / total for e in exps)


def nll(raw: Sequence[Sequence[float]], labels: Sequence[Label], temperature: float) -> float:
    index = {c: i for i, c in enumerate(CLASSES)}
    return -math.fsum(
        math.log(max(softmax(r, temperature)[index[y]], 1e-15))
        for r, y in zip(raw, labels, strict=True)
    ) / len(labels)


def fit_temperature(
    raw: Sequence[Sequence[float]], labels: Sequence[Label], *, iterations: int = 80
) -> float:
    """Золотое сечение по log T на [T_MIN, T_MAX] (log loss по T унимодальна)."""
    if not labels:
        raise ValueError("пустой участок калибровки")
    lo, hi = math.log(T_MIN), math.log(T_MAX)
    a, b = hi - GOLDEN * (hi - lo), lo + GOLDEN * (hi - lo)
    fa, fb = nll(raw, labels, math.exp(a)), nll(raw, labels, math.exp(b))
    for _ in range(iterations):
        if fa < fb:
            hi, b, fb = b, a, fa
            a = hi - GOLDEN * (hi - lo)
            fa = nll(raw, labels, math.exp(a))
        else:
            lo, a, fa = a, b, fb
            b = lo + GOLDEN * (hi - lo)
            fb = nll(raw, labels, math.exp(b))
    return math.exp((lo + hi) / 2)
