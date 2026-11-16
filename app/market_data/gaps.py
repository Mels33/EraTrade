"""Сжатие списка пропущенных свечей в диапазоны для отчёта."""

from datetime import datetime, timedelta


def compress_ranges(times: list[datetime], step: timedelta) -> list[tuple[datetime, datetime]]:
    """[t0, t0+step, t0+2step, t5] → [(t0, t0+2step), (t5, t5)] — границы включительно."""
    ranges: list[tuple[datetime, datetime]] = []
    for t in sorted(times):
        if ranges and t - ranges[-1][1] == step:
            ranges[-1] = (ranges[-1][0], t)
        else:
            ranges.append((t, t))
    return ranges
