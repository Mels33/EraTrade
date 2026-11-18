"""Смоук скользящей проверки 30m на синтетике (короткие окна, без пула процессов)."""

from app.models.walkforward_30m import VARIANTS_30M, walk_forward_30m
from tests.test_pipeline_v2 import bars_30m


def test_walk_forward_30m_smoke() -> None:
    candles = [b.candle for b in bars_30m(6200, seed=71)]  # ≈ 4.3 месяца 30m свечей
    report = walk_forward_30m(
        candles,
        [VARIANTS_30M["vol4"], VARIANTS_30M["barrier8"]],
        trades_per_day=2.0,
        min_train_months=3,
        test_months=1,
        val_days=10,
        cal_days=5,
        catboost_params={"iterations": 30, "od_wait": 10, "thread_count": 2},
        workers=1,
    )
    assert report["folds"] >= 1 and report["rows"] > 5000
    for v in report["variants"]:
        assert set(v["models"]) == {"taker", "maker"}
        for m in v["models"].values():
            total = m["total"]["trades"]
            assert total == m["long"]["trades"] + m["short"]["trades"]
            assert m["fill_rate"] is None or 0 <= m["fill_rate"] <= 1
