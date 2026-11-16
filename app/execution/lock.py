"""Гарантия единственного торгового процесса на API-кошелёк (§9.3 плана).

PostgreSQL session-level advisory lock держится на выделенном соединении всё время
работы процесса. Если процесс упал или соединение потеряно — PostgreSQL снимает
блокировку сам. Соединение в режиме AUTOCOMMIT, чтобы не висеть «idle in transaction».
"""

import hashlib
from types import TracebackType
from typing import Self

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine


class AnotherTraderRunningError(RuntimeError):
    """Блокировку держит другой процесс."""


class TraderLockLostError(RuntimeError):
    """Соединение с блокировкой потеряно — торговать дальше нельзя."""


def lock_key(signer: str) -> int:
    digest = hashlib.sha256(f"eratrade-trader:{signer.lower()}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


class TraderLock:
    def __init__(self, engine: AsyncEngine, signer: str) -> None:
        self._engine = engine
        self.signer = signer.lower()
        self.key = lock_key(signer)
        self._conn: AsyncConnection | None = None

    async def __aenter__(self) -> Self:
        conn = await self._engine.connect()
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        try:
            acquired = (
                await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": self.key})
            ).scalar_one()
        except BaseException:
            await conn.close()
            raise
        if not acquired:
            await conn.close()
            raise AnotherTraderRunningError(
                f"торговый процесс для API-кошелька {self.signer} уже запущен"
            )
        self._conn = conn
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": self.key})
        finally:
            await self._conn.close()
            self._conn = None

    async def ensure_held(self) -> None:
        """Проверяет, что соединение с блокировкой живо. Вызывать перед каждым действием."""
        if self._conn is None:
            raise TraderLockLostError("блокировка не захвачена")
        try:
            await self._conn.execute(text("SELECT 1"))
        except Exception as exc:
            raise TraderLockLostError("соединение с блокировкой потеряно") from exc
