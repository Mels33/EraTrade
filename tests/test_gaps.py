from datetime import UTC, datetime, timedelta

from app.market_data.collector import floor_time
from app.market_data.gaps import compress_ranges

H = timedelta(hours=1)
T0 = datetime(2026, 11, 1, tzinfo=UTC)


def test_compress_ranges() -> None:
    times = [T0, T0 + H, T0 + 2 * H, T0 + 5 * H, T0 + 7 * H, T0 + 8 * H]
    assert compress_ranges(times, H) == [
        (T0, T0 + 2 * H),
        (T0 + 5 * H, T0 + 5 * H),
        (T0 + 7 * H, T0 + 8 * H),
    ]
    assert compress_ranges([], H) == []


def test_floor_time() -> None:
    assert floor_time(T0 + 3 * H + timedelta(minutes=59, seconds=59), H) == T0 + 3 * H
    assert floor_time(T0, H) == T0
