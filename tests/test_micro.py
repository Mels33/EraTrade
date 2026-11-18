"""Модель «спрос и предложение»: архив Binance, минутные строки, признаки v3, проверка."""

import hashlib
import zipfile
from datetime import date
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from app.integrations.binance_public import ArchiveError, download, parse_checksum
from app.research.features_micro import LOOKBACK, compute, feature_set
from app.research.micro_bars import build_day
from app.research.walkforward_micro import VARIANTS, make_labels, walk_forward_micro

DAY = "2026-11-01"
T0_MS = int(pd.Timestamp(DAY, tz="UTC").timestamp() * 1000)


def zip_csv(path: Path, name: str, text: str) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(name, text)
    return path


def test_checksum_parsing() -> None:
    digest = "a" * 64
    assert parse_checksum(f"{digest}  BTCUSDT-aggTrades-2026-11-01.zip\n") == digest
    with pytest.raises(ArchiveError):
        parse_checksum("not-a-hash file.zip")


def test_download_verifies_sha256(tmp_path: Path) -> None:
    payload = b"zip-bytes"
    good = hashlib.sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{good}  f.zip")
        if "2026-11-02" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        path = download(client, "aggTrades", date(2026, 11, 1), tmp_path)
        assert path is not None and path.read_bytes() == payload
        assert download(client, "aggTrades", date(2026, 11, 2), tmp_path) is None

    def bad(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".CHECKSUM"):
            return httpx.Response(200, text=f"{'0' * 64}  f.zip")
        return httpx.Response(200, content=payload)

    with httpx.Client(transport=httpx.MockTransport(bad)) as client, pytest.raises(ArchiveError):
        download(client, "aggTrades", date(2026, 11, 3), tmp_path)


def test_build_day_aggregates_sides_large_and_depth(tmp_path: Path) -> None:
    trades = (
        "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
    )
    rows = [
        (100.0, 1.0, 0, "false"),  # покупка агрессором, notional 100
        (101.0, 2000.0, 10_000, "false"),  # крупная покупка 202 000
        (99.0, 3.0, 30_000, "true"),  # продажа агрессором 297
        (102.0, 1.0, 61_000, "true"),  # следующая минута
    ]
    for k, (px, qty, dt, maker) in enumerate(rows):
        trades += f"{k},{px},{qty},{k},{k},{T0_MS + dt},{maker}\n"
    depth = "timestamp,percentage,depth,notional\n"
    for ts in ("2026-11-01 00:00:30", "2026-11-01 00:01:30"):
        for pct, notional in (
            (-0.2, 500.0),
            (0.2, 300.0),
            (-1, 2000.0),
            (1, 1500.0),
            (-5, 9000.0),
            (5, 8000.0),
        ):
            depth += f"{ts},{pct},1,{notional}\n"
    t_zip = zip_csv(tmp_path / "t.zip", "t.csv", trades)
    d_zip = zip_csv(tmp_path / "d.zip", "d.csv", depth)

    bars = build_day(t_zip, d_zip, DAY)
    assert len(bars) == 1440
    m0 = bars.iloc[0]
    assert m0["open"] == 100.0 and m0["close"] == 99.0 and m0["high"] == 101.0
    assert m0["buy_notional"] == pytest.approx(100 + 202_000)
    assert m0["sell_notional"] == pytest.approx(297)
    assert m0["large_buy_notional"] == pytest.approx(202_000) and m0["large_sell_notional"] == 0
    assert m0["buy_count"] == 2 and m0["sell_count"] == 1
    assert m0["bid_near"] == 500.0 and m0["ask_near"] == 300.0 and m0["depth_age_s"] == 30
    assert m0["deep_bid_5"] == 9000.0
    assert bool(m0["trades_ok"]) and bool(m0["depth_ok"])
    m2 = bars.iloc[2]
    assert not bool(m2["trades_ok"])  # сделок нет
    assert bool(bars.iloc[2]["depth_ok"]) and not bool(bars.iloc[3]["depth_ok"])  # снимок стареет


def synthetic_bars(days: int, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = days * 1440
    minutes = pd.date_range("2026-01-01", periods=n, freq="min", tz="UTC")
    flow = rng.normal(0, 1, n)
    ret = 0.0004 * rng.normal(0, 1, n) + 0.0001 * np.roll(flow, 1)  # поток слегка опережает цену
    close = 80_000 * np.exp(np.cumsum(ret))
    noise = np.abs(rng.normal(0, 0.0003, n)) * close
    buy = np.exp(rng.normal(13, 0.5, n)) * (1 + 0.3 * np.tanh(flow))
    sell = np.exp(rng.normal(13, 0.5, n)) * (1 - 0.3 * np.tanh(flow))
    bid = np.exp(rng.normal(17, 0.2, n))
    ask = np.exp(rng.normal(17, 0.2, n))
    return pd.DataFrame(
        {
            "minute": minutes,
            "open": np.roll(close, 1),
            "high": close + noise,
            "low": close - noise,
            "close": close,
            "vwap": close,
            "buy_notional": buy,
            "sell_notional": sell,
            "buy_count": rng.integers(50, 500, n),
            "sell_count": rng.integers(50, 500, n),
            "large_buy_notional": np.where(rng.random(n) < 0.1, 2e5, 0.0),
            "large_sell_notional": np.where(rng.random(n) < 0.1, 2e5, 0.0),
            "bid_near": bid,
            "ask_near": ask,
            **{f"deep_{s}_{p}": bid * p for s in ("bid", "ask") for p in (1, 2, 3, 4, 5)},
            "trades_ok": True,
            "depth_ok": True,
        }
    )


def test_features_window_equals_history_and_no_lookahead() -> None:
    bars = synthetic_bars(1)
    x_full, ok = compute(bars, depth=True)
    i = 500
    window = bars.iloc[i - LOOKBACK + 1 : i + 1].reset_index(drop=True)
    x_win, ok_win = compute(window, depth=True)
    assert ok[i] and ok_win[-1]
    assert np.array_equal(x_full[i], x_win[-1])  # бит в бит
    tampered = bars.copy()
    tampered.loc[i + 1 :, "buy_notional"] *= 10
    assert np.array_equal(compute(tampered, depth=True)[0][i], x_full[i])
    assert x_full.shape[1] == len(feature_set(depth=True))
    assert compute(bars, depth=False)[0].shape[1] == len(feature_set(depth=False))


def test_validity_requires_depth_only_for_book_model() -> None:
    bars = synthetic_bars(1)
    bars["depth_ok"] = False
    assert compute(bars, depth=False)[1].sum() > 0
    assert compute(bars, depth=True)[1].sum() == 0
    gap = synthetic_bars(1).drop(index=range(300, 305)).reset_index(drop=True)
    _, ok = compute(gap, depth=False)
    assert not ok[300 : 300 + LOOKBACK - 1].any()  # окно, задевшее пропуск минут, невалидно


def test_barrier_labels_first_touch() -> None:
    bars = synthetic_bars(1)
    sigma = np.full(len(bars), 0.0004)
    lab = make_labels(VARIANTS["barrier5"], bars, sigma)
    valid = lab.label >= 0
    assert valid[: len(bars) - 5].all() and not valid[-5:].any()
    up = lab.label == 2
    assert np.allclose(lab.long_ret[up], lab.distance[up])
    assert np.allclose(lab.short_ret[up], -lab.distance[up])


def test_walk_forward_micro_smoke() -> None:
    bars = synthetic_bars(150)
    report = walk_forward_micro(
        bars,
        [VARIANTS["ret5"], VARIANTS["barrier5"]],
        depth=True,
        trades_per_day=(5.0,),
        test_months=1,
        min_train_months=2,
        max_train_months=None,
        val_days=7,
        cal_days=3,
        train_stride=10,
        catboost_params={"iterations": 40, "od_wait": 10, "thread_count": 2},
    )
    assert report["folds"] >= 1 and report["model"] == "book"
    for v in report["variants"]:
        for r in v["results"]:
            assert r["total"]["trades"] == r["long"]["trades"] + r["short"]["trades"]
        assert set(v["random_entry"]) == {"gross", "taker", "maker"}
