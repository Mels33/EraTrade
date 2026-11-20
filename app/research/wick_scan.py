"""Поиск монет для ловли проколов с усреднением (app/research/averaging.py).

Для монет Hyperliquid (топ по обороту) — минутные свечи USDⓈ-M futures из открытого архива
Binance (месячные файлы, sha256). Честная проверка: параметры выбираются на обучающих
месяцах, результат — на следующих (проверочных), которых выбор не видел. Монеты ранжируются
по проверочному результату; итог пишется в JSON для живого бота.
"""

import json
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from app.integrations.binance_public import klines_day, klines_month
from app.research.averaging import AvgParams, run
from app.research.execution_model import Costs

# Альткоины: спред и проскальзывание больше, чем у BTC (стоп — по рынку).
ALT_COSTS = Costs(half_spread=0.0003, slippage=0.0005)
GRID = [
    AvgParams(step=step, levels=3, tp=tp, stop=True)
    for step in (0.008, 0.012, 0.018, 0.025, 0.035)
    for tp in (0.005, 0.01)
]
MIN_TRAIN_CYCLES = 20
MIN_TEST_CYCLES = 8
MAX_TEST_DRAWDOWN = -0.15


def binance_symbol(coin: str) -> str:
    """kPEPE → 1000PEPEUSDT (на Hyperliquid «k» — тысяча монет, на Binance — «1000»)."""
    if coin.startswith("k") and coin[1:].isupper():
        return f"1000{coin[1:]}USDT"
    return f"{coin}USDT"


def months_back(n: int, now: datetime | None = None) -> list[str]:
    """Последние n полных месяцев 'YYYY-MM' по возрастанию."""
    t = (now or datetime.now(UTC)).replace(day=1)
    out = []
    for _ in range(n):
        t = (t - pd.Timedelta(days=1)).replace(day=1)
        out.append(t.strftime("%Y-%m"))
    return sorted(out)


def _parse_zip(path: Path) -> pd.DataFrame:
    with zipfile.ZipFile(path) as z:
        name = z.namelist()[0]
        with z.open(name) as f:
            df = pd.read_csv(
                f, header=None, usecols=[0, 2, 3, 4], names=["t", "high", "low", "close"]
            )
    df = df[pd.to_numeric(df["t"], errors="coerce").notna()]  # строка заголовка (новые файлы)
    return df.astype({"t": "int64", "high": "float64", "low": "float64", "close": "float64"})


@dataclass(frozen=True, slots=True)
class CoinData:
    coin: str
    time: np.ndarray  # мс
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray


def current_month_days(today: date | None = None) -> list[date]:
    """Дни текущего месяца до вчера включительно (дневные файлы появляются на следующий день)."""
    today = today or datetime.now(UTC).date()
    first = today.replace(day=1)
    return [first + timedelta(days=i) for i in range((today - first).days)]


def load_coin(
    client: httpx.Client,
    coin: str,
    months: list[str],
    raw_dir: Path,
    cache_dir: Path,
    days: list[date] | None = None,
) -> CoinData | None:
    days = days or []
    tail = days[-1].isoformat() if days else months[-1]
    cache = cache_dir / f"{coin}-{months[0]}-{tail}.npz"
    if cache.exists():
        d = np.load(cache)
        return CoinData(coin, d["time"], d["high"], d["low"], d["close"])
    symbol = binance_symbol(coin)
    frames = []
    for month in months:
        path = klines_month(client, symbol, month, raw_dir)
        if path is None:
            continue
        frames.append(_parse_zip(path))
        path.unlink()
    for day in days:
        path = klines_day(client, symbol, day, raw_dir)
        if path is None:
            continue
        frames.append(_parse_zip(path))
        path.unlink()
    if not frames:
        return None
    df = pd.concat(frames).drop_duplicates("t").sort_values("t")
    cache.parent.mkdir(parents=True, exist_ok=True)
    for old in cache_dir.glob(f"{coin}-*.npz"):  # прежние срезы этой монеты больше не нужны
        old.unlink()
    arrays = {k: df[k].to_numpy() for k in ("high", "low", "close")}
    np.savez_compressed(cache, time=df["t"].to_numpy(), **arrays)
    return CoinData(coin, df["t"].to_numpy(), arrays["high"], arrays["low"], arrays["close"])


@dataclass
class CoinResult:
    coin: str
    params: AvgParams | None
    train_total: float | None
    test_total: float | None
    test_drawdown: float | None
    test_cycles: int
    test_wins: int
    test_months: int

    @property
    def passed(self) -> bool:
        return (
            self.test_total is not None
            and self.test_total > 0
            and self.test_cycles >= MIN_TEST_CYCLES
            and (self.test_drawdown or 0) > MAX_TEST_DRAWDOWN
        )


def evaluate(data: CoinData, split_ms: int, costs: Costs) -> CoinResult:
    train = data.time < split_ms
    test = ~train
    test_months = (
        int((data.time[test][-1] - data.time[test][0]) // (30 * 86_400_000)) if test.any() else 0
    )
    best: tuple[float, AvgParams] | None = None
    if train.sum() < 60 * 1440:  # меньше двух месяцев истории до проверки — не оцениваем
        return CoinResult(data.coin, None, None, None, None, 0, 0, test_months)
    for p in GRID:
        r = run(data.high[train], data.low[train], data.close[train], p, costs)
        if r.cycles < MIN_TRAIN_CYCLES:
            continue
        score = r.total
        if best is None or score > best[0]:
            best = (score, p)
    if best is None or not test.any():
        return CoinResult(data.coin, None, None, None, None, 0, 0, test_months)
    p = best[1]
    r = run(data.high[test], data.low[test], data.close[test], p, costs)
    return CoinResult(data.coin, p, best[0], r.total, r.max_drawdown, r.cycles, r.wins, test_months)


def scan(
    coins: list[str],
    *,
    months: list[str],
    test_months: int,
    raw_dir: Path,
    cache_dir: Path,
    workers: int = 6,
    days: list[date] | None = None,
    progress: Callable[[str], None] | None = None,
) -> list[CoinResult]:
    split = months[-test_months]
    split_ms = int(pd.Timestamp(f"{split}-01", tz="UTC").timestamp() * 1000)

    def one(coin: str) -> CoinResult | None:
        with httpx.Client(timeout=120) as client:
            data = load_coin(client, coin, months, raw_dir, cache_dir, days)
        if data is None or len(data.close) < 30 * 1440:
            if progress:
                progress(f"{coin}: нет данных на Binance")
            return None
        costs = Costs() if coin == "BTC" else ALT_COSTS
        res = evaluate(data, split_ms, costs)
        if progress:
            tt = "—" if res.test_total is None else f"{res.test_total:+.1%}"
            progress(f"{coin}: проверка {tt}, циклов {res.test_cycles}, параметры {res.params}")
        return res

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = [r for r in pool.map(one, coins) if r is not None]
    return sorted(results, key=lambda r: (r.passed, r.test_total or -9), reverse=True)


def save(
    results: list[CoinResult],
    path: Path,
    *,
    months: list[str],
    test_months: int,
    days: list[date] | None = None,
    universe: int | None = None,
) -> None:
    payload: dict[str, Any] = {
        "at": datetime.now(UTC).isoformat(),
        "train": [months[0], months[-test_months - 1]],
        "test": [months[-test_months], days[-1].isoformat() if days else months[-1]],
        "universe": universe,
        "coins": [
            {**asdict(r), "params": asdict(r.params) if r.params else None, "passed": r.passed}
            for r in results
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")  # атомарно: бот может читать файл в этот момент
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=lambda o: o.item())
    )  # numpy-числа → обычные
    tmp.replace(path)
