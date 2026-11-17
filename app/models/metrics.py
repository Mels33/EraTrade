"""Метрики вероятностной классификации (§14.3): без numpy, детерминированно."""

import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from app.features.target import CLASSES, Label

EPS = 1e-15


def class_distribution(labels: Sequence[Label]) -> dict[str, float]:
    counts = Counter(labels)
    total = len(labels)
    return {c.value: (counts[c] / total if total else 0.0) for c in CLASSES}


def log_loss(labels: Sequence[Label], probs: Sequence[Sequence[float]]) -> float:
    """Средняя многоклассовая log loss; probs[i] — вероятности в порядке CLASSES."""
    if not labels:
        return math.nan
    index = {c: i for i, c in enumerate(CLASSES)}
    return -math.fsum(
        math.log(min(max(p[index[y]], EPS), 1.0)) for y, p in zip(labels, probs, strict=True)
    ) / len(labels)


def brier(labels: Sequence[Label], probs: Sequence[Sequence[float]]) -> float:
    """Многоклассовый Brier score: средняя сумма квадратов ошибок по классам."""
    if not labels:
        return math.nan
    total = math.fsum(
        math.fsum((pk - (1.0 if CLASSES[k] == y else 0.0)) ** 2 for k, pk in enumerate(p))
        for y, p in zip(labels, probs, strict=True)
    )
    return total / len(labels)


@dataclass(frozen=True, slots=True)
class BaselineReport:
    """Baseline «априорные частоты train»: любая модель должна быть лучше него."""

    prior: dict[str, float]
    log_loss: float
    brier: float


def prior_baseline(train: Sequence[Label], evaluate: Sequence[Label]) -> BaselineReport:
    prior = class_distribution(train)
    probs = [[prior[c.value] for c in CLASSES]] * len(evaluate)
    return BaselineReport(
        prior=prior, log_loss=log_loss(evaluate, probs), brier=brier(evaluate, probs)
    )
