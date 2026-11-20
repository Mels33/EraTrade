"""Часовые свечи BTC из минутных строк архива Binance (app/research/micro_bars.py).

Колонки: open/high/low/close, vwap, buy/sell — объём агрессивных покупок/продаж ($), volume,
deep_bid_k/deep_ask_k — глубина стакана ±k% на конец часа (последняя валидная минута),
depth_ok — глубина есть. Кэш — csv.gz (pyarrow в проекте нет).
"""

from pathlib import Path

import pandas as pd

from app.research.micro_bars import DEEP, load_bars

DEEP_COLUMNS = [f"deep_{side}_{int(k)}" for side in ("bid", "ask") for k in DEEP]


def to_hourly(bars: pd.DataFrame) -> pd.DataFrame:
    b = bars.set_index("minute")
    notional = b["buy_notional"] + b["sell_notional"]
    g = b.resample("1h", label="left", closed="left")
    out = pd.DataFrame(
        {
            "open": g["open"].first(),
            "high": g["high"].max(),
            "low": g["low"].min(),
            "close": g["close"].last(),
            "buy": g["buy_notional"].sum(),
            "sell": g["sell_notional"].sum(),
        }
    )
    out["volume"] = out["buy"] + out["sell"]
    vw = (b["vwap"] * notional).resample("1h", label="left", closed="left").sum()
    out["vwap"] = (vw / out["volume"]).where(out["volume"] > 0, out["close"])
    # depth_ok в минутных строках — про ближнюю глубину ±0.2% (есть только с 2026);
    # глубина ±1..5% есть с 2023 — берём минуты, где она записана.
    depth = b.loc[b[DEEP_COLUMNS].notna().all(axis=1), DEEP_COLUMNS]
    last_depth = depth.resample("1h", label="left", closed="left").last()
    out = out.join(last_depth)
    out["depth_ok"] = out[DEEP_COLUMNS].notna().all(axis=1)
    out = out.dropna(subset=["open", "high", "low", "close"])
    out.index.name = "hour"
    return out


def load_hourly(minute_dir: Path, cache: Path) -> pd.DataFrame:
    """Часовые свечи; кэш пересобирается, если появились новые минутные файлы."""
    files = sorted(minute_dir.glob("*-1m-*.csv.gz"))
    if not files:
        raise FileNotFoundError(f"нет минутных файлов в {minute_dir} — eratrade micro-data")
    if cache.exists() and cache.stat().st_mtime >= max(f.stat().st_mtime for f in files):
        hourly = pd.read_csv(cache, index_col="hour", parse_dates=["hour"])
        hourly.index = pd.to_datetime(hourly.index, utc=True)
        hourly["depth_ok"] = hourly["depth_ok"].astype(bool)
        return hourly
    hourly = to_hourly(load_bars(minute_dir))
    cache.parent.mkdir(parents=True, exist_ok=True)
    hourly.to_csv(cache)
    return hourly
