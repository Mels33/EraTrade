"""Торговая политика: пороги, таблица решений docs/spec.md §2, приоритет выхода по времени."""

from dataclasses import replace
from datetime import timedelta

import pytest

from app.domain.account import Side
from app.models.predictor import Prediction
from app.signals.policy import Decision, PolicyParams, PositionContext, decide, model_direction
from tests.factories import HOUR, T0

PARAMS = PolicyParams(
    min_probability=0.45,
    min_margin=0.15,
    min_edge=0.00195,
    allow_long=True,
    allow_short=True,
    time_exit_candles=8,
    reentry_cooldown_candles=1,
    step=HOUR,
)
NOW = T0 + 100 * HOUR
FLAT = PositionContext(side=None)


def pred(down: float, flat: float, up: float, edge_bp: float) -> Prediction:
    return Prediction("v", {"DOWN": down, "FLAT": flat, "UP": up}, (0.0, 0.0, 0.0), edge_bp / 1e4)


BUY = pred(0.10, 0.35, 0.55, 40)
SELL = pred(0.55, 0.35, 0.10, -40)
WEAK = pred(0.30, 0.38, 0.32, 1)


@pytest.mark.parametrize(
    ("p", "expected"),
    [
        (BUY, Side.BUY),
        (SELL, Side.SELL),
        (WEAK, None),
        (pred(0.35, 0.20, 0.45, 40), None),  # отрыв 0.10 < 0.15
        (pred(0.05, 0.50, 0.44, 40), None),  # p(UP) < 0.45
        (pred(0.10, 0.35, 0.55, 15), None),  # E[r] 15 bp < издержки + запас 19.5 bp
    ],
)
def test_model_direction_thresholds(p: Prediction, expected: Side | None) -> None:
    assert model_direction(p, PARAMS)[0] is expected


@pytest.mark.parametrize(
    ("side", "p", "expected"),
    [
        (None, BUY, Decision.BUY),
        (None, SELL, Decision.SELL),
        (None, WEAK, Decision.HOLD),
        (Side.BUY, BUY, Decision.HOLD),  # не увеличиваем
        (Side.BUY, SELL, Decision.EXIT),  # закрыть без разворота
        (Side.BUY, WEAK, Decision.HOLD),
        (Side.SELL, SELL, Decision.HOLD),
        (Side.SELL, BUY, Decision.EXIT),
    ],
)
def test_decision_table(side: Side | None, p: Prediction, expected: Decision) -> None:
    position = PositionContext(side=side, opened_at=NOW - 2 * HOUR if side else None)
    assert decide(p, position, NOW, PARAMS).decision is expected


def test_time_exit_has_priority_over_model_and_errors() -> None:
    old = PositionContext(side=Side.BUY, opened_at=NOW - 8 * HOUR + timedelta(seconds=5))
    assert decide(BUY, old, NOW, PARAMS).decision is Decision.EXIT
    assert decide(None, old, NOW, PARAMS, error="нет данных").decision is Decision.EXIT
    young = PositionContext(side=Side.BUY, opened_at=NOW - 7 * HOUR + timedelta(seconds=5))
    assert decide(BUY, young, NOW, PARAMS).decision is Decision.HOLD


def test_error_gives_hold() -> None:
    result = decide(None, FLAT, NOW, PARAMS, error="признаки: пропуск свечей")
    assert result.decision is Decision.HOLD and "пропуск свечей" in result.reason


def test_reentry_cooldown() -> None:
    # выход в 10:00:05 (сразу после закрытия свечи) — вход не раньше решения в 11:00
    closed = PositionContext(side=None, last_closed_at=NOW + timedelta(seconds=5))
    assert decide(BUY, closed, NOW, PARAMS).decision is Decision.HOLD
    assert decide(BUY, closed, NOW + HOUR, PARAMS).decision is Decision.BUY
    stopped = PositionContext(side=None, last_closed_at=NOW - timedelta(minutes=30))
    assert decide(BUY, stopped, NOW, PARAMS).decision is Decision.BUY


def test_disallowed_side() -> None:
    no_short = replace(PARAMS, allow_short=False)
    assert decide(SELL, FLAT, NOW, no_short).decision is Decision.HOLD
    # закрыть long противоположным сигналом можно и при запрете short
    long = PositionContext(side=Side.BUY, opened_at=NOW - HOUR)
    assert decide(SELL, long, NOW, no_short).decision is Decision.EXIT
