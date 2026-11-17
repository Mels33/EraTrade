"""Dead man's switch через scheduleCancel (§12.5 плана).

Биржа отменит все открытые ордера в заданный момент, если процесс не продлит расписание.
Ограничения биржи: время не раньше чем через 5 с, не больше 10 срабатываний в сутки.

ВАЖНО: срабатывание отменяет и защитные стопы, а позицию не закрывает. При наших типах
ордеров (вход — IOC, в книге стоят только стопы) это снимет защиту, поэтому в конфиге
выключено (dead_mans_switch.enabled = false). Нужен только при лимитных входах (GTC/ALO).
"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import logfire

from app.config.strategy import DeadMansSwitchConfig
from app.execution.session import TradingSession
from app.integrations.hyperliquid import actions
from app.integrations.hyperliquid.parsing import datetime_to_ms

MIN_LEAD = timedelta(seconds=5)


class DeadMansSwitchError(RuntimeError):
    pass


def _utc_now() -> datetime:
    return datetime.now(UTC)


class DeadMansSwitch:
    def __init__(
        self,
        session: TradingSession,
        config: DeadMansSwitchConfig,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.session = session
        self.config = config
        self.clock = clock
        self.scheduled_for: datetime | None = None

    async def arm(self) -> datetime:
        at = self.clock() + timedelta(seconds=self.config.horizon_seconds)
        if at - self.clock() < MIN_LEAD:
            raise DeadMansSwitchError("время отмены должно быть минимум через 5 секунд")
        response = await self.session.submit(actions.schedule_cancel_action(datetime_to_ms(at)))
        if not response.ok:
            raise DeadMansSwitchError(f"биржа не приняла scheduleCancel: {response.error}")
        self.scheduled_for = at
        return at

    async def disarm(self) -> None:
        response = await self.session.submit(actions.schedule_cancel_action(None))
        if not response.ok:
            raise DeadMansSwitchError(f"биржа не сняла scheduleCancel: {response.error}")
        self.scheduled_for = None

    async def run(self) -> None:
        """Продлевает расписание каждые refresh_seconds; при остановке — снимает его."""
        if not self.config.enabled:
            logfire.info("dead man's switch выключен в конфиге")
            return
        try:
            while True:
                await self.arm()
                await asyncio.sleep(self.config.refresh_seconds)
        finally:
            if self.scheduled_for is not None:
                await self.disarm()
