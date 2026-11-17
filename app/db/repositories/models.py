"""Реестр версий моделей, снимки признаков и сигналы (§15.4).

Правила:
- одна active и не больше одной shadow (гарантирует и частичный уникальный индекс);
- активировать можно только версию, прошедшую shadow, или ранее активную (откат);
- каждая смена статуса пишет аудит в той же транзакции;
- смена статуса блокирует строки (FOR UPDATE), генерация сигнала читает их FOR SHARE —
  модель не меняется посреди обработки одного сигнала.
"""

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import FeatureSnapshotRecord, ModelVersionRecord, SignalRecord
from app.db.repositories.control import record_audit
from app.domain.control import Actor
from app.features.pipeline import FeatureInput, FeatureVector
from app.models.artifact import ModelArtifact


class ModelStatus(StrEnum):
    CANDIDATE = "candidate"
    SHADOW = "shadow"
    ACTIVE = "active"
    RETIRED = "retired"


class RegistryError(ValueError):
    pass


async def register_model(
    session: AsyncSession, artifact: ModelArtifact, *, actor: Actor, artifact_path: str
) -> ModelVersionRecord:
    if await session.get(ModelVersionRecord, artifact.version) is not None:
        raise RegistryError(f"версия {artifact.version} уже зарегистрирована")
    record = ModelVersionRecord(
        version=artifact.version,
        status=ModelStatus.CANDIDATE.value,
        feature_set_version=artifact.feature_set_version,
        model_sha256=artifact.model_sha256,
        artifact_path=artifact_path,
        artifact=artifact.meta,
    )
    session.add(record)
    await session.flush()
    await record_audit(
        session,
        actor=actor,
        action="model.registered",
        details={"version": artifact.version, "model_sha256": artifact.model_sha256},
    )
    return record


async def list_models(session: AsyncSession) -> list[ModelVersionRecord]:
    stmt = select(ModelVersionRecord).order_by(ModelVersionRecord.created_at.desc())
    return list((await session.execute(stmt)).scalars())


async def get_model(
    session: AsyncSession, version: str, *, for_update: bool = False
) -> ModelVersionRecord:
    stmt = select(ModelVersionRecord).where(ModelVersionRecord.version == version)
    if for_update:
        stmt = stmt.with_for_update()
    record = (await session.execute(stmt)).scalar_one_or_none()
    if record is None:
        raise RegistryError(f"версии {version} нет в реестре")
    return record


async def models_for_signal(
    session: AsyncSession,
) -> tuple[ModelVersionRecord | None, ModelVersionRecord | None]:
    """(active, shadow), заблокированные FOR SHARE до конца транзакции сигнала."""
    stmt = (
        select(ModelVersionRecord)
        .where(ModelVersionRecord.status.in_([ModelStatus.ACTIVE, ModelStatus.SHADOW]))
        .with_for_update(read=True)
    )
    records = list((await session.execute(stmt)).scalars())
    active = next((r for r in records if r.status == ModelStatus.ACTIVE), None)
    shadow = next((r for r in records if r.status == ModelStatus.SHADOW), None)
    return active, shadow


async def _current(session: AsyncSession, status: ModelStatus) -> ModelVersionRecord | None:
    stmt = (
        select(ModelVersionRecord)
        .where(ModelVersionRecord.status == status.value)
        .with_for_update()
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def set_shadow(
    session: AsyncSession, version: str, *, actor: Actor, reason: str
) -> ModelVersionRecord:
    """Перевести версию в shadow. Прежняя shadow-версия возвращается в candidate."""
    record = await get_model(session, version, for_update=True)
    if record.status not in (ModelStatus.CANDIDATE, ModelStatus.RETIRED):
        raise RegistryError(f"в shadow можно перевести candidate или retired, а не {record.status}")
    previous = await _current(session, ModelStatus.SHADOW)
    if previous is not None:
        previous.status = ModelStatus.CANDIDATE.value
        await session.flush()
    record.status = ModelStatus.SHADOW.value
    record.shadow_at = datetime.now(UTC)
    await session.flush()
    await record_audit(
        session,
        actor=actor,
        action="model.shadow",
        reason=reason,
        details={"version": version, "replaced": previous.version if previous else None},
    )
    return record


async def activate(
    session: AsyncSession, version: str, *, actor: Actor, reason: str
) -> ModelVersionRecord:
    """Сделать версию активной. Прежняя активная уходит в retired (доступна для отката)."""
    record = await get_model(session, version, for_update=True)
    rollback = record.status == ModelStatus.RETIRED and record.activated_at is not None
    if record.status != ModelStatus.SHADOW and not rollback:
        raise RegistryError(
            "активировать можно только версию после shadow или ранее активную (откат); "
            f"статус {version}: {record.status}"
        )
    previous = await _current(session, ModelStatus.ACTIVE)
    if previous is not None:
        previous.status = ModelStatus.RETIRED.value
        previous.retired_at = datetime.now(UTC)
        await session.flush()
    record.status = ModelStatus.ACTIVE.value
    record.activated_at = datetime.now(UTC)
    record.retired_at = None
    await session.flush()
    await record_audit(
        session,
        actor=actor,
        action="model.rollback" if rollback else "model.activated",
        reason=reason,
        details={"version": version, "previous": previous.version if previous else None},
    )
    return record


async def retire(
    session: AsyncSession, version: str, *, actor: Actor, reason: str
) -> ModelVersionRecord:
    record = await get_model(session, version, for_update=True)
    if record.status == ModelStatus.RETIRED:
        raise RegistryError(f"{version} уже retired")
    old = record.status
    record.status = ModelStatus.RETIRED.value
    record.retired_at = datetime.now(UTC)
    await session.flush()
    await record_audit(
        session,
        actor=actor,
        action="model.retired",
        reason=reason,
        details={"version": version, "previous_status": old},
    )
    return record


# --- снимки признаков и сигналы ---


async def save_feature_snapshot(
    session: AsyncSession, market_id: int, inp: FeatureInput, vector: FeatureVector
) -> int:
    """Идемпотентно: один снимок на (рынок, момент решения, версия признаков)."""
    insert_stmt = insert(FeatureSnapshotRecord).values(
        market_id=market_id,
        decision_time=vector.decision_time,
        feature_set_version=vector.version,
        values=vector.as_dict(),
        input_hash=inp.hash(),
        last_candle_open_time=inp.candles[-1].open_time,
    )
    stmt = insert_stmt.on_conflict_do_nothing(
        index_elements=["market_id", "decision_time", "feature_set_version"]
    ).returning(FeatureSnapshotRecord.id)
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        return int(new_id)
    existing = await session.execute(
        select(FeatureSnapshotRecord).where(
            FeatureSnapshotRecord.market_id == market_id,
            FeatureSnapshotRecord.decision_time == vector.decision_time,
            FeatureSnapshotRecord.feature_set_version == vector.version,
        )
    )
    record = existing.scalar_one()
    if record.input_hash != inp.hash():
        raise RegistryError(
            "снимок признаков на этот момент уже сохранён с другими входными данными"
        )
    return record.id


async def get_signal(
    session: AsyncSession, market_id: int, decision_time: datetime, model_version: str
) -> SignalRecord | None:
    stmt = select(SignalRecord).where(
        SignalRecord.market_id == market_id,
        SignalRecord.decision_time == decision_time,
        SignalRecord.model_version == model_version,
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_signal(session: AsyncSession, **fields: Any) -> SignalRecord:
    record = SignalRecord(**fields)
    session.add(record)
    await session.flush()
    return record


async def list_signals(session: AsyncSession, *, limit: int = 50) -> list[SignalRecord]:
    stmt = select(SignalRecord).order_by(SignalRecord.decision_time.desc(), SignalRecord.id.desc())
    return list((await session.execute(stmt.limit(limit))).scalars())


async def signals_without_outcome(
    session: AsyncSession, market_id: int, *, decided_before: datetime
) -> list[SignalRecord]:
    stmt = (
        select(SignalRecord)
        .where(
            SignalRecord.market_id == market_id,
            SignalRecord.outcome_return.is_(None),
            SignalRecord.decision_time <= decided_before,
        )
        .order_by(SignalRecord.decision_time)
    )
    return list((await session.execute(stmt)).scalars())


# --- исполнение active-сигнала (этап 12) ---


async def claim_execution(session: AsyncSession, signal_id: int, now: datetime) -> bool:
    """Атомарно забирает сигнал на исполнение. False — его уже обрабатывали (в т.ч. процесс,
    упавший посреди исполнения): повторно не исполняем, ордер разбирают tracker и сверка."""
    stmt = (
        update(SignalRecord)
        .where(SignalRecord.id == signal_id, SignalRecord.execution_status.is_(None))
        .values(execution_status="executing", execution_started_at=now)
        .returning(SignalRecord.id)
    )
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def finish_execution(
    session: AsyncSession,
    signal_id: int,
    *,
    status: str,
    detail: str,
    now: datetime,
    order_id: uuid.UUID | None = None,
) -> None:
    await session.execute(
        update(SignalRecord)
        .where(SignalRecord.id == signal_id)
        .values(
            execution_status=status,
            execution_detail=detail[:2000],
            execution_order_id=order_id,
            execution_finished_at=now,
        )
    )
