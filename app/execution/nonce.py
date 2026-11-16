"""Выдача nonce для подписей (§9.3 плана).

Nonce Hyperliquid ведутся по подписывающему кошельку и должны быть уникальны; биржа
принимает значения в окне около текущего времени. Выдача атомарна в PostgreSQL:

    nonce = max(текущее время в мс, последний выданный + 1)

Это переживает рестарт (последнее значение в БД) и даже ошибочный второй процесс
(UPDATE ... RETURNING сериализуется на строке), а процессная блокировка исключает гонки
внутри процесса.
"""

import asyncio
import time
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import NonceStateRecord

# Насколько выданный nonce может опережать часы. Больше — признак сбоя часов:
# биржа отклонит такие nonce, а продолжать выдавать их бессмысленно.
MAX_NONCE_LEAD_MS = 60 * 60 * 1000


class NonceError(RuntimeError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


class NonceManager:
    def __init__(
        self,
        sessionmaker: async_sessionmaker[AsyncSession],
        signer: str,
        *,
        clock_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self._sessionmaker = sessionmaker
        self.signer = signer.lower()
        self._clock_ms = clock_ms
        self._lock = asyncio.Lock()

    async def next(self) -> int:
        async with self._lock:
            now = self._clock_ms()
            insert_stmt = insert(NonceStateRecord).values(signer=self.signer, last_nonce=now)
            stmt = insert_stmt.on_conflict_do_update(
                index_elements=["signer"],
                set_={
                    "last_nonce": func.greatest(
                        NonceStateRecord.last_nonce + 1, insert_stmt.excluded.last_nonce
                    ),
                    "updated_at": func.now(),
                },
            ).returning(NonceStateRecord.last_nonce)
            async with self._sessionmaker.begin() as session:
                nonce: int = (await session.execute(stmt)).scalar_one()
            if nonce - now > MAX_NONCE_LEAD_MS:
                raise NonceError(
                    f"последний nonce опережает часы на {(nonce - now) / 1000:.0f} с — "
                    "проверьте синхронизацию системного времени"
                )
            return nonce

    async def last(self) -> int | None:
        async with self._sessionmaker() as session:
            stmt = select(NonceStateRecord.last_nonce).where(NonceStateRecord.signer == self.signer)
            return (await session.execute(stmt)).scalar_one_or_none()
