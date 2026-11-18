"""Скользящая проверка: окна не заглядывают в будущее, отчёт собирается по всем окнам."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import pairwise

from app.models.experiment import DEFAULT_VARIANTS, Trade
from app.models.walkforward import make_folds, summarize, walk_forward
from tests.factories import HOUR, T0, candle_series, funding_series

PURGE = 8 * HOUR


def test_folds_are_ordered_and_purged() -> None:
    first = datetime(2022, 1, 8, tzinfo=UTC)
    last = datetime(2026, 11, 17, tzinfo=UTC)
    folds = make_folds(first, last, purge=PURGE)
    assert folds[0].test[0] == datetime(2023, 7, 1, tzinfo=UTC)  # 18 мес. обучения минимум
    for f in folds:
        assert f.train_end + PURGE <= f.val[0]
        assert f.val[1] + PURGE <= f.cal[0]
        assert f.cal[1] + PURGE <= f.test[0]
        assert f.val[1] - f.val[0] == timedelta(days=60)
        assert f.cal[1] - f.cal[0] == timedelta(days=30)
    for a, b in pairwise(folds):
        assert a.test[1] == b.test[0]  # окна проверки идут подряд, без дыр и наложений
    assert folds[-1].test[1] > last


def test_summarize_t_stat() -> None:
    trades = [Trade(T0, "long", x) for x in (0.01, 0.012, 0.009, 0.011)]
    stats = summarize(trades, days=4)
    assert stats["trades"] == 4 and stats["t_stat"] is not None and stats["t_stat"] > 10
    assert summarize([], days=4)["t_stat"] is None


def test_walk_forward_smoke() -> None:
    hl_start = T0
    aux_candles = candle_series(9000, start=hl_start - 9000 * HOUR, seed=51)
    aux_funding = funding_series(hl_start - 9040 * HOUR, hl_start + 10 * HOUR, seed=52)
    candles = candle_series(1500, start=hl_start, seed=53)
    funding = funding_series(hl_start - 30 * HOUR, hl_start + 1510 * HOUR, seed=54)
    messages: list[str] = []
    report = walk_forward(
        (candles, funding),
        (aux_candles, aux_funding),
        [DEFAULT_VARIANTS["fixed4"], DEFAULT_VARIANTS["barrier8"]],
        cost_bps=Decimal("14.5"),
        edge_bps=Decimal("19.5"),
        trades_per_day=1.0,
        min_train_months=6,
        test_months=3,
        catboost_params={"iterations": 30, "od_wait": 10},
        progress=messages.append,
    )
    assert report["folds"] >= 2 and messages
    assert report["rows_by_source"]["binance"] > 0 and report["rows_by_source"]["hyperliquid"] > 0
    for v in report["variants"]:
        assert v["folds"], v["variant"]
        assert v["total"]["trades"] == sum(f["trades"] for f in v["folds"])
        for f in v["folds"]:
            assert f["train_rows"] > 0
        assert v["random_entry"]["long_mean_net"] is not None
