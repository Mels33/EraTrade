"""Вспомогательный источник Binance: почасовой funding, сверка источников, добавление в train."""

import dataclasses
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from app.config.strategy import load_strategy
from app.domain.market import DataSource
from app.features.dataset import (
    TargetParams,
    build_dataset,
    chronological_split,
    compare_sources,
    with_auxiliary,
    write_dataset,
)
from app.features.pipeline import FEATURE_NAMES
from app.integrations.binance import BinanceFuturesClient, hourly_funding
from app.integrations.hyperliquid.parsing import datetime_to_ms
from app.models.artifact import load_artifact
from app.models.training import load_dataset_dir, train_model
from app.signals.policy import PolicyParams
from tests.factories import HOUR, T0, candle_series, funding_series

ROOT = Path(__file__).resolve().parents[1]
STRATEGY = load_strategy(ROOT / "config" / "strategy.toml")
TARGET = TargetParams(4, Decimal("39.5"), Decimal("14.5"))


def test_hourly_funding_uses_last_known_rate_divided_by_8() -> None:
    rates = [(T0, Decimal("0.0001")), (T0 + 8 * HOUR, Decimal("0.0002"))]
    premium = [(T0 + i * HOUR, Decimal(i) / 10_000) for i in range(1, 11)]
    points = hourly_funding(rates, premium)
    assert [p.time for p in points] == [t for t, _ in premium]
    assert points[0].funding_rate == Decimal("0.0000125")  # 0.01% / 8
    assert points[6].funding_rate == Decimal("0.0000125")  # T0+7h — ставка T0
    assert points[7].funding_rate == Decimal("0.000025")  # T0+8h — новая ставка
    assert points[2].premium == Decimal("0.0003")


def test_hourly_funding_skips_gaps_and_unknown_start() -> None:
    rates = [(T0 + 2 * HOUR, Decimal("0.0001"))]
    premium = [(T0 + i * HOUR, Decimal(0)) for i in range(0, 14)]
    points = hourly_funding(rates, premium)
    assert points[0].time == T0 + 2 * HOUR  # до первой ставки точек нет
    assert points[-1].time == T0 + 11 * HOUR  # дальше 9 ч без новой ставки — не выдумываем


async def test_client_parses_and_paginates() -> None:
    start = T0
    klines = [
        [
            datetime_to_ms(start + i * HOUR),
            "100",
            "101",
            "99",
            "100.5",
            "12.5",
            datetime_to_ms(start + (i + 1) * HOUR) - 1,
            "0",
            77,
            "0",
            "0",
            "0",
        ]
        for i in range(2000)
    ]
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        calls.append(params)
        begin = int(params["startTime"])
        page = [k for k in klines if k[0] >= begin][: int(params["limit"])]
        return httpx.Response(200, content=json.dumps(page))

    client = BinanceFuturesClient(
        "https://example.invalid", transport=httpx.MockTransport(handler), pause=0
    )
    async with client:
        candles = await client.candles(start, start + 2000 * HOUR, now=start + 2000 * HOUR)
    assert len(candles) == 2000 and len(calls) == 2
    c = candles[0]
    assert c.close == Decimal("100.5") and c.trades_count == 77 and c.source is DataSource.IMPORT
    assert c.close_time == c.open_time + HOUR and c.is_closed


def _shifted_funding(points: list) -> list:  # type: ignore[type-arg]
    """«Другая биржа»: funding и premium сдвинуты и зашумлены, цена та же."""
    return [
        dataclasses.replace(
            p, funding_rate=p.funding_rate * 3 + Decimal("0.0001"), premium=-p.premium
        )
        for p in points
    ]


def test_compare_sources_flags_divergent_features() -> None:
    candles = candle_series(400)
    funding = funding_series(T0 - 30 * HOUR, T0 + 410 * HOUR)
    primary = build_dataset(candles, funding, TARGET)
    aux = build_dataset(candles, _shifted_funding(funding), TARGET, source="binance")
    report = compare_sources(primary.rows, aux.rows)
    assert report["overlap_rows"] == len(primary.rows)
    assert report["label_agreement"] == 1.0
    assert "premium_last_bps" in report["weak_features"]
    assert "ret_1" not in report["weak_features"]


def test_auxiliary_rows_only_in_train_and_before_primary(tmp_path: Path) -> None:
    aux_candles = candle_series(900, start=T0 - 600 * HOUR, seed=21)
    aux_funding = funding_series(T0 - 640 * HOUR, T0 + 310 * HOUR, seed=22)
    candles = candle_series(500, seed=23)
    funding = funding_series(T0 - 30 * HOUR, T0 + 510 * HOUR, seed=24)
    horizon = 4 * HOUR

    primary = build_dataset(candles, funding, TARGET)
    parts = chronological_split(primary.rows, horizon)
    aux = build_dataset(aux_candles, aux_funding, TARGET, source="binance")
    combined, new_parts = with_auxiliary(primary, parts, aux, horizon=horizon, name="binance")

    first_primary = primary.rows[0].decision_time
    aux_rows = [r for r in combined.rows if r.source == "binance"]
    assert aux_rows and all(r.decision_time <= first_primary - horizon for r in aux_rows)
    by_name = {p.name: p for p in new_parts}
    assert all(r.source == "binance" for r in by_name["train"].rows[: len(aux_rows)])
    for name in ("validation", "calibration", "test"):
        assert all(r.source == "hyperliquid" for r in by_name[name].rows)
    assert combined.auxiliary is not None and combined.auxiliary["rows_in_train"] == len(aux_rows)

    _, manifest = write_dataset(
        combined, new_parts, tmp_path / "ds", market={"coin": "BTC"}, project_root=ROOT
    )
    assert manifest["rows_by_source"] == {
        "binance": len(aux_rows),
        "hyperliquid": len(primary.rows),
    }
    _, loaded = load_dataset_dir(tmp_path / "ds")
    assert {s.source for s in loaded["train"]} == {"binance", "hyperliquid"}
    assert {s.source for s in loaded["test"]} == {"hyperliquid"}


def test_training_ignores_divergent_features(tmp_path: Path) -> None:
    candles = candle_series(1200, seed=3)
    funding = funding_series(T0 - 30 * HOUR, T0 + 1210 * HOUR, seed=5)
    aux_candles = candle_series(1500, start=T0 - 1300 * HOUR, seed=31)
    aux_funding = _shifted_funding(funding_series(T0 - 1340 * HOUR, T0 + 210 * HOUR, seed=32))
    horizon = 4 * HOUR
    primary = build_dataset(candles, funding, TARGET)
    parts = chronological_split(primary.rows, horizon)
    aux = build_dataset(aux_candles, aux_funding, TARGET, source="binance")
    combined, new_parts = with_auxiliary(primary, parts, aux, horizon=horizon, name="binance")
    write_dataset(combined, new_parts, tmp_path / "ds", market={"coin": "BTC"}, project_root=ROOT)

    artifact = train_model(
        tmp_path / "ds",
        STRATEGY,
        PolicyParams.from_strategy(STRATEGY, HOUR),
        tmp_path / "models",
        catboost_params={"iterations": 40, "od_wait": 10},
    )
    ignored = artifact.meta["ignored_features"]
    assert ignored == sorted(combined.auxiliary["overlap"]["weak_features"])  # type: ignore[index]
    importance = artifact.meta["feature_importance"]
    assert all(importance[name] == 0 for name in ignored)
    assert load_artifact(artifact.path).feature_names == FEATURE_NAMES
