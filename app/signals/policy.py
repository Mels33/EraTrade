"""Торговая политика (§15.2–15.3, docs/spec.md §2): вероятности → BUY / SELL / HOLD / EXIT.

Порядок:
1. Выход по времени важнее модели (stop-loss стоит на бирже и срабатывает сам).
2. Ошибка признаков/модели → HOLD (открытая позиция остаётся под защитой стопа).
3. Направление модели: p ≥ min_probability, отрыв от противоположного класса ≥ min_margin,
   ожидаемая доходность за горизонт выше издержек + запаса. Иначе — нет направления.
4. Таблица решений: без позиции — вход (с учётом cooldown и разрешённых сторон);
   с позицией — противоположный сигнал закрывает (без разворота), тот же — ничего.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum

from app.config.strategy import StrategyConfig
from app.domain.account import Side
from app.models.predictor import Prediction

POLICY_VERSION = "p1"


class Decision(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    EXIT = "EXIT"


@dataclass(frozen=True, slots=True)
class PositionContext:
    side: Side | None  # BUY — long, SELL — short, None — позиции нет
    opened_at: datetime | None = None
    last_closed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PolicyParams:
    min_probability: float
    min_margin: float
    min_edge: float  # доля: издержки + запас
    allow_long: bool
    allow_short: bool
    time_exit_candles: int
    reentry_cooldown_candles: int
    step: timedelta

    @classmethod
    def from_strategy(cls, s: StrategyConfig, step: timedelta) -> "PolicyParams":
        edge_bps: Decimal = s.target.cost_bps + s.target.safety_margin_bps
        return cls(
            min_probability=float(s.signal.min_probability),
            min_margin=float(s.signal.min_margin),
            min_edge=float(edge_bps) / 10_000,
            allow_long=s.policy.allow_long,
            allow_short=s.policy.allow_short,
            time_exit_candles=s.policy.time_exit_candles,
            reentry_cooldown_candles=s.policy.reentry_cooldown_candles,
            step=step,
        )


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    decision: Decision
    reason: str
    direction: Side | None = None  # направление модели (до таблицы решений)


def model_direction(pred: Prediction, p: PolicyParams) -> tuple[Side | None, str]:
    up, down, edge = pred.p_up, pred.p_down, pred.expected_return
    if up >= p.min_probability and up - down >= p.min_margin and edge > p.min_edge:
        return Side.BUY, f"p(UP)={up:.2f}, отрыв {up - down:.2f}, E[r]={edge * 1e4:.1f} bp"
    if down >= p.min_probability and down - up >= p.min_margin and -edge > p.min_edge:
        return Side.SELL, f"p(DOWN)={down:.2f}, отрыв {down - up:.2f}, E[r]={edge * 1e4:.1f} bp"
    best = max(up, down)
    return None, (
        f"нет уверенного направления: p(UP)={up:.2f}, p(DOWN)={down:.2f}, "
        f"E[r]={edge * 1e4:.1f} bp (порог p {p.min_probability:.2f}, "
        f"край {p.min_edge * 1e4:.1f} bp, max {best:.2f})"
    )


def _floor(t: datetime, step: timedelta) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=t.tzinfo)
    return epoch + ((t - epoch) // step) * step


def decide(
    pred: Prediction | None,
    position: PositionContext,
    decision_time: datetime,
    p: PolicyParams,
    *,
    error: str | None = None,
) -> PolicyDecision:
    # 1. выход по времени
    if position.side is not None and position.opened_at is not None:
        held = (decision_time - _floor(position.opened_at, p.step)) // p.step
        if held >= p.time_exit_candles:
            return PolicyDecision(Decision.EXIT, f"выход по времени: {held} свечей в позиции")

    # 2. ошибка данных или модели
    if pred is None:
        return PolicyDecision(Decision.HOLD, f"HOLD из-за ошибки: {error or 'нет прогноза'}")

    direction, why = model_direction(pred, p)

    # 4. таблица решений
    if position.side is None:
        if direction is None:
            return PolicyDecision(Decision.HOLD, why)
        if direction is Side.BUY and not p.allow_long:
            return PolicyDecision(Decision.HOLD, f"long запрещён политикой; {why}", direction)
        if direction is Side.SELL and not p.allow_short:
            return PolicyDecision(Decision.HOLD, f"short запрещён политикой; {why}", direction)
        if position.last_closed_at is not None:
            allowed_from = (
                _floor(position.last_closed_at, p.step) + p.reentry_cooldown_candles * p.step
            )
            if decision_time < allowed_from:
                return PolicyDecision(
                    Decision.HOLD,
                    f"cooldown после выхода до {allowed_from:%Y-%m-%d %H:%M}; {why}",
                    direction,
                )
        decision = Decision.BUY if direction is Side.BUY else Decision.SELL
        return PolicyDecision(decision, why, direction)

    if direction is None:
        return PolicyDecision(Decision.HOLD, f"держим позицию; {why}")
    if direction is position.side:
        return PolicyDecision(
            Decision.HOLD, f"позиция уже открыта, не увеличиваем; {why}", direction
        )
    return PolicyDecision(
        Decision.EXIT, f"противоположный сигнал — закрыть без разворота; {why}", direction
    )
