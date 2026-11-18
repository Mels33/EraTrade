"""Минутные строки «спрос и предложение» из дневных архивов Binance.

Одна строка на минуту (UTC, по началу минуты):
- по сделкам: open/high/low/close, vwap; объём и notional покупок / продаж агрессором
  (is_buyer_maker = false — покупатель ударил по ask), число сделок, крупные сделки
  (notional ≥ LARGE) по сторонам, максимальная сделка;
- по стакану: последний снимок bookDepth не позже конца минуты — notional заявок на
  покупку (bid) и продажу (ask) в пределах ±0.2% (воспроизводится вживую публичным
  стаканом) и глубокие уровни ±1…5% (колонки deep_*, только для исследования: вживую без
  накопления их не получить);
- trades_ok — в минуте были сделки; depth_ok — есть снимок ближней глубины (±0.2%) не
  старше 2 минут. Уровень ±0.2% публикуется в архиве только с января 2026 года; глубокие
  уровни ±1…5% — с 2023.
"""

import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

LARGE_NOTIONAL = 100_000.0
NEAR = 0.2
DEEP = (1.0, 2.0, 3.0, 4.0, 5.0)
MAX_DEPTH_AGE_S = 120

COLUMNS = [
    "minute",
    "open",
    "high",
    "low",
    "close",
    "vwap",
    "buy_qty",
    "sell_qty",
    "buy_notional",
    "sell_notional",
    "buy_count",
    "sell_count",
    "large_buy_notional",
    "large_sell_notional",
    "max_trade_notional",
    "bid_near",
    "ask_near",
    *[f"deep_bid_{p:g}" for p in DEEP],
    *[f"deep_ask_{p:g}" for p in DEEP],
    "depth_age_s",
    "trades_ok",
    "depth_ok",
]


def _read_csv_from_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            return pd.read_csv(f)


def trade_minutes(trades: pd.DataFrame) -> pd.DataFrame:
    t = trades
    price = t["price"].astype(float)
    qty = t["quantity"].astype(float)
    notional = price * qty
    is_buy = ~t["is_buyer_maker"].astype(str).str.lower().eq("true")
    minute = pd.to_datetime(t["transact_time"], unit="ms", utc=True).dt.floor("min")
    frame = pd.DataFrame(
        {
            "minute": minute,
            "price": price,
            "qty": qty,
            "notional": notional,
            "buy_qty": qty.where(is_buy, 0.0),
            "sell_qty": qty.where(~is_buy, 0.0),
            "buy_notional": notional.where(is_buy, 0.0),
            "sell_notional": notional.where(~is_buy, 0.0),
            "buy_count": is_buy.astype(int),
            "sell_count": (~is_buy).astype(int),
            "large_buy_notional": notional.where(is_buy & (notional >= LARGE_NOTIONAL), 0.0),
            "large_sell_notional": notional.where(~is_buy & (notional >= LARGE_NOTIONAL), 0.0),
        }
    )
    g = frame.groupby("minute", sort=True)
    out = g.agg(
        open=("price", "first"),
        high=("price", "max"),
        low=("price", "min"),
        close=("price", "last"),
        qty=("qty", "sum"),
        notional_sum=("notional", "sum"),
        buy_qty=("buy_qty", "sum"),
        sell_qty=("sell_qty", "sum"),
        buy_notional=("buy_notional", "sum"),
        sell_notional=("sell_notional", "sum"),
        buy_count=("buy_count", "sum"),
        sell_count=("sell_count", "sum"),
        large_buy_notional=("large_buy_notional", "sum"),
        large_sell_notional=("large_sell_notional", "sum"),
        max_trade_notional=("notional", "max"),
    )
    out["vwap"] = out["notional_sum"] / out["qty"]
    return out.drop(columns=["qty", "notional_sum"])


def depth_minutes(depth: pd.DataFrame, minutes: pd.DatetimeIndex) -> pd.DataFrame:
    """Для каждой минуты — последний снимок глубины с timestamp < конца минуты."""
    d = depth.copy()
    d["ts"] = pd.to_datetime(d["timestamp"], utc=True)
    d["pct"] = d["percentage"].astype(float).round(2)
    wide = d.pivot_table(index="ts", columns="pct", values="notional", aggfunc="last").sort_index()
    snaps = pd.DataFrame(index=wide.index)
    snaps["bid_near"] = wide.get(-NEAR)
    snaps["ask_near"] = wide.get(NEAR)
    for p in DEEP:
        snaps[f"deep_bid_{p:g}"] = wide.get(-p)
        snaps[f"deep_ask_{p:g}"] = wide.get(p)
    snaps["snap_ts"] = snaps.index
    ends = pd.DataFrame({"minute": minutes, "end": minutes + pd.Timedelta(minutes=1)})
    merged = pd.merge_asof(
        ends.sort_values("end"),
        snaps.reset_index(drop=True).sort_values("snap_ts"),
        left_on="end",
        right_on="snap_ts",
        direction="backward",
        allow_exact_matches=False,
    )
    merged["depth_age_s"] = (merged["end"] - merged["snap_ts"]).dt.total_seconds()
    return merged.drop(columns=["end", "snap_ts"]).set_index("minute")


def build_day(trades_zip: Path, depth_zip: Path | None, day: str) -> pd.DataFrame:
    trades = trade_minutes(_read_csv_from_zip(trades_zip))
    minutes = pd.date_range(f"{day} 00:00", periods=1440, freq="min", tz="UTC")
    bars = trades.reindex(minutes)
    if depth_zip is not None:
        depth = depth_minutes(_read_csv_from_zip(depth_zip), minutes)
        bars = bars.join(depth)
    else:
        for col in (
            "bid_near",
            "ask_near",
            *[f"deep_{s}_{p:g}" for s in ("bid", "ask") for p in DEEP],
        ):
            bars[col] = np.nan
        bars["depth_age_s"] = np.nan
    bars.index.name = "minute"
    bars = bars.reset_index()
    bars["trades_ok"] = bars["close"].notna()
    bars["depth_ok"] = (
        bars["depth_age_s"].le(MAX_DEPTH_AGE_S) & bars["bid_near"].gt(0) & bars["ask_near"].gt(0)
    )
    for col in ("buy_count", "sell_count"):
        bars[col] = bars[col].fillna(0).astype(int)
    return bars[COLUMNS]


def bars_path(out_dir: Path, day: str, symbol: str = "BTCUSDT") -> Path:
    return out_dir / f"{symbol}-1m-{day}.csv.gz"


def load_bars(out_dir: Path) -> pd.DataFrame:
    files = sorted(out_dir.glob("*-1m-*.csv.gz"))
    if not files:
        raise FileNotFoundError(f"нет минутных файлов в {out_dir} — eratrade micro-data")
    frames = [pd.read_csv(f, parse_dates=["minute"]) for f in files]
    bars = pd.concat(frames, ignore_index=True).sort_values("minute").reset_index(drop=True)
    bars["minute"] = pd.to_datetime(bars["minute"], utc=True)
    return bars
