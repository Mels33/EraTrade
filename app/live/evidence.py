"""Доказательства из shadow перед активацией модели (§15.4, §17).

Модель можно сделать active, только если в shadow она:
- дала достаточно сигналов с наступившим горизонтом;
- почти не давала HOLD из-за ошибок данных/модели;
- покрыла почти все закрытые свечи;
- по накопленным результатам лучше априорного baseline по log loss (если требуется).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.strategy import LiveConfig
from app.db.models import ModelVersionRecord, SignalRecord
from app.signals.report import model_section


@dataclass(frozen=True, slots=True)
class Evidence:
    version: str
    failures: list[str]
    section: dict[str, Any] | None

    @property
    def ok(self) -> bool:
        return not self.failures


async def shadow_evidence(
    session: AsyncSession,
    model: ModelVersionRecord,
    live: LiveConfig,
    *,
    now: datetime,
    step: timedelta,
    cost_bps: float,
) -> Evidence:
    stmt = select(SignalRecord).where(SignalRecord.model_version == model.version)
    if model.shadow_at is not None:
        stmt = stmt.where(SignalRecord.decision_time >= model.shadow_at - step)
    signals = list((await session.execute(stmt.order_by(SignalRecord.decision_time))).scalars())
    if not signals:
        return Evidence(model.version, ["в shadow нет ни одного сигнала"], None)
    expected = int((now - signals[0].decision_time) // step) + 1
    section = model_section(model, signals, cost=cost_bps / 10_000, expected_candles=expected)

    failures: list[str] = []
    resolved = section["calibration"]["resolved"]
    if resolved < live.min_shadow_outcomes:
        failures.append(f"результатов в shadow {resolved} из нужных {live.min_shadow_outcomes}")
    errors = sum(section["feature_or_model_errors"].values())
    error_rate = errors / len(signals)
    if error_rate > float(live.max_shadow_error_rate):
        failures.append(
            f"HOLD из-за ошибок {error_rate:.1%} > {float(live.max_shadow_error_rate):.0%}"
        )
    coverage = section["coverage"] or 0.0
    if coverage < float(live.min_shadow_coverage):
        failures.append(f"покрытие свечей {coverage:.0%} < {float(live.min_shadow_coverage):.0%}")
    cal = section["calibration"]
    if live.require_beats_baseline:
        if cal.get("log_loss") is None:
            failures.append("нет результатов для сравнения с baseline")
        elif cal["log_loss"] >= cal["baseline_log_loss"]:
            failures.append(
                f"log loss в shadow {cal['log_loss']:.4f} не лучше baseline "
                f"{cal['baseline_log_loss']:.4f}"
            )
    return Evidence(model.version, failures, section)
