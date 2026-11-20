"""Измеренный adverse selection для выбора рынков маркет-мейкера."""

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.research.mm_backtest import BacktestResult


def load_adverse_selection(path: Path) -> dict[str, Decimal]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ожидался JSON-объект по монетам")
    result = {}
    for coin, raw in data.items():
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: {coin}: ожидался объект измерения")
        value = Decimal(str(raw["adverse_selection_bps"]))
        if not value.is_finite() or value < 0:
            raise ValueError(f"{path}: {coin}: adverse_selection_bps должен быть ≥ 0")
        result[coin] = value
    return result


def save_adverse_selection(result: BacktestResult, path: Path, *, horizon_seconds: int) -> None:
    markout = next((m for m in result.markouts if m.horizon_seconds == horizon_seconds), None)
    if markout is None:
        raise ValueError(f"горизонт {horizon_seconds} с не рассчитывался")
    if markout.fills == 0:
        raise ValueError(f"нет markout {horizon_seconds} с: недостаточно данных после исполнений")
    data: dict[str, Any] = {}
    if path.exists():
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"{path}: ожидался JSON-объект по монетам")
        data = loaded
    data[result.coin] = {
        "horizon_seconds": horizon_seconds,
        "fills": markout.fills,
        "mean_markout_bps": str(markout.mean_bps),
        "adverse_selection_bps": str(markout.adverse_selection_bps),
        "measured_from": result.start.isoformat(),
        "measured_until": result.end.isoformat(),
        "backtest_fills": result.fills,
        "net_pnl_usd": str(result.net_pnl_usd),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".part")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
