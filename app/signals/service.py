"""Генерация сигнала на закрытую свечу (§15.1).

1–4. данные, свежесть, признаки, проверка (NaN/inf/границы) → иначе HOLD;
5. снимок признаков; 6–7. вероятности active- и shadow-модели и сырой прогноз;
8–9. торговая политика с учётом текущей позиции → сигнал в БД.
Передача намерения в risk engine и исполнение — этапы 12–14 (shadow-сигнал не исполняется
никогда). Один сигнал на (рынок, момент решения, версия модели): повтор возвращает сохранённый.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FillRecord, ModelVersionRecord, SignalRecord
from app.db.repositories import market as market_repo
from app.db.repositories import models as models_repo
from app.domain.account import Side
from app.features.loader import features_at
from app.features.pipeline import FeatureError, FeatureInput, FeatureVector
from app.features.target import CLASSES, forward_return, label_for
from app.models.artifact import ArtifactError, load_artifact
from app.models.predictor import OutOfBoundsError, Prediction, Predictor
from app.signals.policy import (
    POLICY_VERSION,
    Decision,
    PolicyParams,
    PositionContext,
    decide,
)


class PredictorCache:
    """Загруженные модели по версии. Хеш файла сверяется с реестром при загрузке."""

    def __init__(self, models_root: Path) -> None:
        self.models_root = models_root
        self._cache: dict[str, Predictor] = {}

    def get(self, record: ModelVersionRecord) -> Predictor:
        cached = self._cache.get(record.version)
        if cached is not None:
            return cached
        path = Path(record.artifact_path)
        if not path.is_absolute():
            path = self.models_root / path
        artifact = load_artifact(path)
        if artifact.version != record.version or artifact.model_sha256 != record.model_sha256:
            raise ArtifactError(f"артефакт {path} не совпадает с записью реестра {record.version}")
        predictor = Predictor(artifact)
        self._cache[record.version] = predictor
        return predictor


@dataclass(frozen=True, slots=True)
class SignalDecision:
    role: str  # active | shadow
    version: str
    decision: Decision
    reason: str
    probabilities: dict[str, float] | None
    expected_return_bps: float | None
    signal_id: int | None = None
    replay: bool = False


@dataclass(frozen=True, slots=True)
class SignalResult:
    decision_time: datetime
    feature_error: str | None
    input_hash: str | None
    decisions: list[SignalDecision]

    @property
    def active(self) -> SignalDecision | None:
        return next((d for d in self.decisions if d.role == "active"), None)


def _from_record(role: str, r: SignalRecord) -> SignalDecision:
    return SignalDecision(
        role=role,
        version=r.model_version,
        decision=Decision(r.decision),
        reason=r.reason,
        # JSONB не хранит порядок ключей — восстанавливаем порядок CLASSES
        probabilities=(
            {c.value: float(r.probabilities[c.value]) for c in CLASSES} if r.probabilities else None
        ),
        expected_return_bps=r.expected_return_bps,
        signal_id=r.id,
        replay=True,
    )


async def position_context(
    session: AsyncSession, market_id: int, side: Side | None
) -> PositionContext:
    """Время открытия текущей позиции и последнего выхода — по исполнениям в БД.
    side — текущая сторона позиции по данным биржи (источник истины)."""
    stmt = (
        select(FillRecord.time, FillRecord.start_position, FillRecord.side, FillRecord.size)
        .where(FillRecord.market_id == market_id)
        .order_by(FillRecord.time, FillRecord.tid)
    )
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    for time, start, fill_side, size in (await session.execute(stmt)).all():
        end = start + (size if fill_side == Side.BUY.value else -size)
        if start == 0 and end != 0:
            opened_at = time
        if start != 0 and (end == 0 or (start > 0) != (end > 0)):
            closed_at = time
            opened_at = time if end != 0 else None
    return PositionContext(
        side=side, opened_at=opened_at if side is not None else None, last_closed_at=closed_at
    )


async def generate_signal(
    session: AsyncSession,
    *,
    market_id: int,
    interval: str,
    step: timedelta,
    params: PolicyParams,
    position: PositionContext,
    predictors: PredictorCache,
    decision_time: datetime | None = None,
    now: datetime | None = None,
    max_delay: timedelta | None = None,
    persist: bool = True,
) -> SignalResult:
    active, shadow = await models_repo.models_for_signal(session)

    if decision_time is None:
        last = await market_repo.get_last_closed_candle(session, market_id, interval)
        if last is None:
            raise FeatureError("в БД нет закрытых свечей")
        decision_time = last.close_time

    inp: FeatureInput | None = None
    vector: FeatureVector | None = None
    error: str | None = None
    if now is not None and max_delay is not None and now - decision_time > max_delay:
        error = f"решение опаздывает: {(now - decision_time).total_seconds():.0f} с после закрытия"
    else:
        try:
            inp, vector = await features_at(session, market_id, interval, step, decision_time)
        except FeatureError as exc:
            error = f"признаки: {exc}"

    snapshot_id: int | None = None
    if persist and inp is not None and vector is not None:
        snapshot_id = await models_repo.save_feature_snapshot(session, market_id, inp, vector)

    decisions: list[SignalDecision] = []
    for role, record in (("active", active), ("shadow", shadow)):
        if record is None:
            continue
        if persist:
            existing = await models_repo.get_signal(
                session, market_id, decision_time, record.version
            )
            if existing is not None:
                decisions.append(_from_record(role, existing))
                continue
        pred: Prediction | None = None
        model_error = error
        if vector is not None:
            try:
                pred = predictors.get(record).predict(vector)
            except OutOfBoundsError as exc:
                model_error = f"вне границ: {exc}"
            except (ArtifactError, OSError) as exc:
                model_error = f"модель: {exc}"
        policy = decide(pred, position, decision_time, params, error=model_error)
        probs = pred.probabilities if pred else None
        edge = pred.expected_return * 1e4 if pred else None
        signal_id = None
        if persist:
            saved = await models_repo.add_signal(
                session,
                market_id=market_id,
                decision_time=decision_time,
                model_version=record.version,
                model_role=role,
                policy_version=POLICY_VERSION,
                feature_snapshot_id=snapshot_id,
                probabilities=probs,
                expected_return_bps=edge,
                decision=policy.decision.value,
                reason=policy.reason,
                position_side=position.side.value if position.side else None,
                horizon_candles=int(record.artifact["target"]["horizon_candles"]),
            )
            signal_id = saved.id
        decisions.append(
            SignalDecision(
                role, record.version, policy.decision, policy.reason, probs, edge, signal_id
            )
        )
    return SignalResult(
        decision_time=decision_time,
        feature_error=error,
        input_hash=inp.hash() if inp else None,
        decisions=decisions,
    )


async def fill_outcomes(
    session: AsyncSession,
    *,
    market_id: int,
    interval: str,
    step: timedelta,
    threshold_bps: Decimal,
    now: datetime,
) -> int:
    """Фактический результат сигналов, у которых наступил горизонт: доходность и класс."""
    last = await market_repo.get_last_closed_candle(session, market_id, interval)
    if last is None:
        return 0
    filled = 0
    for signal in await models_repo.signals_without_outcome(
        session, market_id, decided_before=last.close_time
    ):
        exit_close_time = signal.decision_time + signal.horizon_candles * step
        if exit_close_time > last.close_time:
            continue
        candles = {
            c.close_time: c
            for c in await market_repo.get_candles(
                session,
                market_id,
                interval,
                start=signal.decision_time - step,
                end=exit_close_time,
            )
        }
        entry, exit_ = candles.get(signal.decision_time), candles.get(exit_close_time)
        if entry is None or exit_ is None:
            continue  # пропуск в данных — результат не заполняем
        ret = forward_return(entry.close, exit_.close)
        signal.outcome_return = ret
        signal.outcome_label = label_for(ret, threshold_bps).value
        signal.outcome_at = now
        filled += 1
    await session.flush()
    return filled


def signal_summary(result: SignalResult) -> dict[str, Any]:
    return {
        "decision_time": result.decision_time.isoformat(),
        "feature_error": result.feature_error,
        "input_hash": result.input_hash,
        "decisions": [
            {
                "role": d.role,
                "version": d.version,
                "decision": d.decision.value,
                "reason": d.reason,
                "probabilities": d.probabilities,
                "expected_return_bps": d.expected_return_bps,
                "replay": d.replay,
            }
            for d in result.decisions
        ],
    }
