"""Прогоны сверки, расхождения, funding/ledger и ожидаемая позиция. Без commit."""

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    FillRecord,
    FundingEventRecord,
    LedgerEventRecord,
    PositionBaselineRecord,
    ReconciliationDifferenceRecord,
    ReconciliationRunRecord,
)
from app.domain.account import FundingEvent, LedgerEvent


async def start_run(session: AsyncSession, market_id: int, trigger: str) -> ReconciliationRunRecord:
    run = ReconciliationRunRecord(market_id=market_id, trigger=trigger, status="running")
    session.add(run)
    await session.flush()
    return run


async def finish_run(
    session: AsyncSession,
    run_id: int,
    *,
    status: str,
    finished_at: datetime,
    summary: dict[str, Any],
    differences: Sequence[dict[str, Any]],
) -> None:
    run = await session.get(ReconciliationRunRecord, run_id)
    if run is None:
        raise LookupError(f"прогон сверки {run_id} не найден")
    run.status = status
    run.finished_at = finished_at
    run.summary = summary
    for diff in differences:
        session.add(ReconciliationDifferenceRecord(run_id=run_id, **diff))
    await session.flush()


async def latest_run(
    session: AsyncSession, market_id: int, *, finished_only: bool = False
) -> ReconciliationRunRecord | None:
    stmt = (
        select(ReconciliationRunRecord)
        .where(ReconciliationRunRecord.market_id == market_id)
        .order_by(ReconciliationRunRecord.id.desc())
        .limit(1)
    )
    if finished_only:
        stmt = stmt.where(ReconciliationRunRecord.status != "running")
    return (await session.execute(stmt)).scalar_one_or_none()


async def mark_abandoned_runs(session: AsyncSession, market_id: int, finished_at: datetime) -> int:
    """Прогоны, оставшиеся в running после остановки процесса, помечаются как прерванные."""
    stmt = (
        update(ReconciliationRunRecord)
        .where(
            ReconciliationRunRecord.market_id == market_id,
            ReconciliationRunRecord.status == "running",
        )
        .values(
            status="error",
            finished_at=finished_at,
            summary={"error": "прерван: процесс сверки был остановлен во время прогона"},
        )
    )
    result = await session.execute(stmt)
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


async def list_differences(
    session: AsyncSession, run_id: int
) -> list[ReconciliationDifferenceRecord]:
    stmt = (
        select(ReconciliationDifferenceRecord)
        .where(ReconciliationDifferenceRecord.run_id == run_id)
        .order_by(ReconciliationDifferenceRecord.id)
    )
    return list((await session.execute(stmt)).scalars())


async def add_funding_events(session: AsyncSession, events: Sequence[FundingEvent]) -> int:
    if not events:
        return 0
    rows = [
        {
            "hash": e.hash,
            "coin": e.coin,
            "time": e.time,
            "usdc": e.usdc,
            "position_size": e.position_size,
            "funding_rate": e.funding_rate,
            "raw": e.raw,
        }
        for e in events
    ]
    stmt = (
        insert(FundingEventRecord)
        .values(rows)
        .on_conflict_do_nothing()
        .returning(FundingEventRecord.hash)
    )
    return len((await session.execute(stmt)).all())


async def add_ledger_events(session: AsyncSession, events: Sequence[LedgerEvent]) -> int:
    if not events:
        return 0
    rows = [
        {"hash": e.hash, "delta_type": e.delta_type, "time": e.time, "usdc": e.usdc, "raw": e.raw}
        for e in events
    ]
    stmt = (
        insert(LedgerEventRecord)
        .values(rows)
        .on_conflict_do_nothing()
        .returning(LedgerEventRecord.hash)
    )
    return len((await session.execute(stmt)).all())


async def last_event_time(session: AsyncSession, model: Any) -> datetime | None:
    value: datetime | None = (await session.execute(select(func.max(model.time)))).scalar_one()
    return value


async def last_fill_time(session: AsyncSession, market_id: int) -> datetime | None:
    stmt = select(func.max(FillRecord.time)).where(FillRecord.market_id == market_id)
    value: datetime | None = (await session.execute(stmt)).scalar_one()
    return value


async def latest_baseline(session: AsyncSession, market_id: int) -> PositionBaselineRecord | None:
    stmt = (
        select(PositionBaselineRecord)
        .where(PositionBaselineRecord.market_id == market_id)
        .order_by(PositionBaselineRecord.id.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


async def add_baseline(
    session: AsyncSession,
    market_id: int,
    *,
    size: Decimal,
    effective_at: datetime,
    actor: str,
    reason: str,
) -> PositionBaselineRecord:
    record = PositionBaselineRecord(
        market_id=market_id, size=size, effective_at=effective_at, actor=actor, reason=reason
    )
    session.add(record)
    await session.flush()
    return record


_SIGNED_SIZE = case((FillRecord.side == "buy", FillRecord.size), else_=-FillRecord.size)


async def expected_position(
    session: AsyncSession, market_id: int
) -> tuple[Decimal, datetime | None]:
    """Позиция, которую бот ожидает: baseline + исполнения ордеров бота после baseline."""
    baseline = await latest_baseline(session, market_id)
    stmt = select(func.coalesce(func.sum(_SIGNED_SIZE), 0)).where(
        FillRecord.market_id == market_id, FillRecord.order_id.is_not(None)
    )
    if baseline is not None:
        stmt = stmt.where(FillRecord.time > baseline.effective_at)
    delta = Decimal((await session.execute(stmt)).scalar_one())
    base = baseline.size if baseline else Decimal(0)
    return base + delta, baseline.effective_at if baseline else None


async def foreign_fills(
    session: AsyncSession, market_id: int, since: datetime | None
) -> list[FillRecord]:
    """Исполнения не по ордерам бота (ручная торговля, ликвидация) после since."""
    stmt = select(FillRecord).where(
        FillRecord.market_id == market_id, FillRecord.order_id.is_(None)
    )
    if since is not None:
        stmt = stmt.where(FillRecord.time > since)
    return list((await session.execute(stmt.order_by(FillRecord.time))).scalars())
