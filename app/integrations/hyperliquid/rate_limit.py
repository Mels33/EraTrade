"""Клиентский учёт веса REST-запросов (Rate limits and user limits).

Биржа: 1200 единиц веса в минуту на IP. Мы по умолчанию используем только часть бюджета,
чтобы оставить запас для отмены ордеров и других процессов на том же IP.
Полноценный централизованный limiter с резервом под отмены — этап 8.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable

IP_WEIGHT_PER_MINUTE = 1200

# Все процессы бота работают с одного IP: бюджеты в сумме меньше лимита биржи (1100 < 1200),
# запас — для ручных запросов и отмен. Отмены идут через отдельный бюджет /exchange.
SERVICE_WEIGHT_BUDGETS: dict[str, int] = {
    "collector": 200,
    "reconciler": 300,
    "trading": 600,
}

# Базовый вес info-запросов. Всё, чего нет в таблице, — 20.
INFO_WEIGHTS: dict[str, int] = {
    "l2Book": 2,
    "allMids": 2,
    "clearinghouseState": 2,
    "orderStatus": 2,
    "spotClearinghouseState": 2,
    "exchangeStatus": 2,
    "userRole": 60,
}
DEFAULT_INFO_WEIGHT = 20

# Дополнительная единица веса за каждые N элементов ответа.
ITEMS_PER_EXTRA_WEIGHT: dict[str, int] = {
    "candleSnapshot": 60,
    "userFills": 20,
    "userFillsByTime": 20,
    "recentTrades": 20,
    "historicalOrders": 20,
    "fundingHistory": 20,
    "userFunding": 20,
    "nonUserFundingUpdates": 20,
}


def info_weight(request_type: str) -> int:
    return INFO_WEIGHTS.get(request_type, DEFAULT_INFO_WEIGHT)


def extra_weight(request_type: str, items_returned: int) -> int:
    per = ITEMS_PER_EXTRA_WEIGHT.get(request_type)
    return items_returned // per if per else 0


class TokenBucket:
    """Асинхронный token bucket. Допускает «долг»: вес, известный только после ответа,
    списывается постфактум, и следующие запросы ждут, пока долг не восстановится."""

    def __init__(
        self,
        capacity: float,
        refill_per_second: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = capacity
        self._clock = clock
        self._sleep = sleep
        self._updated = clock()
        self._lock = asyncio.Lock()

    @classmethod
    def per_minute(
        cls,
        weight: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> "TokenBucket":
        return cls(weight, weight / 60, clock=clock, sleep=sleep)

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(
            self.capacity, self._tokens + (now - self._updated) * self.refill_per_second
        )
        self._updated = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, weight: float) -> None:
        if weight > self.capacity:
            raise ValueError(f"вес {weight} больше ёмкости {self.capacity}")
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= weight:
                    self._tokens -= weight
                    return
                await self._sleep((weight - self._tokens) / self.refill_per_second)

    def charge(self, weight: float) -> None:
        """Списать вес без ожидания (может увести баланс в минус)."""
        self._refill()
        self._tokens -= weight
