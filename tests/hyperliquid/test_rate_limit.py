import pytest

from app.integrations.hyperliquid.rate_limit import (
    TokenBucket,
    extra_weight,
    info_weight,
)


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_weights_from_docs() -> None:
    assert info_weight("allMids") == 2
    assert info_weight("clearinghouseState") == 2
    assert info_weight("orderStatus") == 2
    assert info_weight("meta") == 20
    assert info_weight("candleSnapshot") == 20
    assert info_weight("userRole") == 60
    assert extra_weight("candleSnapshot", 5000) == 83
    assert extra_weight("userFillsByTime", 2000) == 100
    assert extra_weight("meta", 1000) == 0


async def test_bucket_waits_when_empty() -> None:
    t = FakeTime()
    bucket = TokenBucket.per_minute(60, clock=t.clock, sleep=t.sleep)  # 1 вес/с
    await bucket.acquire(60)
    assert t.sleeps == []
    await bucket.acquire(10)
    assert t.sleeps == [pytest.approx(10)]


async def test_charge_creates_debt() -> None:
    t = FakeTime()
    bucket = TokenBucket.per_minute(60, clock=t.clock, sleep=t.sleep)
    await bucket.acquire(50)
    bucket.charge(30)  # вес, известный после ответа: баланс -20
    assert bucket.available == pytest.approx(-20)
    await bucket.acquire(5)
    assert sum(t.sleeps) == pytest.approx(25)


async def test_refill_capped_at_capacity() -> None:
    t = FakeTime()
    bucket = TokenBucket.per_minute(60, clock=t.clock, sleep=t.sleep)
    t.now += 3600
    assert bucket.available == 60


async def test_weight_above_capacity_rejected() -> None:
    bucket = TokenBucket.per_minute(10)
    with pytest.raises(ValueError):
        await bucket.acquire(11)
