"""Барьер автоматического исполнения (этап 15, §17.3).

`trader --auto` запускается, только если одновременно:
- в конфиге явно включено: auto.enabled = true и execution.require_manual_confirmation = false;
- проверка готовности (live-check) без FAIL;
- ручной этап пройден: не меньше auto.min_manual_trades закрытых сделок бота,
  и ни у одной нет замечаний разбора (размер, стоп, номинал).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.strategy import StrategyConfig
from app.live.readiness import Check, CheckStatus
from app.live.review import round_trips


async def auto_failures(
    session: AsyncSession, market_id: int, strategy: StrategyConfig, checks: list[Check]
) -> list[str]:
    failures: list[str] = []
    if not strategy.auto.enabled:
        failures.append("auto.enabled = false в config/strategy.toml")
    if strategy.execution.require_manual_confirmation:
        failures.append("execution.require_manual_confirmation = true — сначала ручной этап")
    failures.extend(
        f"live-check: {c.name} — {c.detail}" for c in checks if c.status is CheckStatus.FAIL
    )
    trips = [t for t in await round_trips(session, market_id, strategy) if t.bot_entry]
    closed = [t for t in trips if t.closed_at is not None]
    if len(closed) < strategy.auto.min_manual_trades:
        failures.append(
            f"закрытых сделок бота {len(closed)} из нужных {strategy.auto.min_manual_trades}"
        )
    flagged = [t for t in trips if t.checks]
    if flagged:
        failures.append(
            f"сделок с замечаниями разбора: {len(flagged)} (последняя "
            f"{flagged[-1].opened_at:%Y-%m-%d %H:%M}: {'; '.join(flagged[-1].checks)})"
        )
    return failures
