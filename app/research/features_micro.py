"""Признаки v3 «спрос и предложение» на минутных строках (решение каждую минуту).

Только то, что воспроизводится вживую без накопления:
- поток агрессивных сделок (публичный поток aggTrade = архив aggTrades);
- ближняя глубина стакана ±0.2% (публичный стакан limit=1000 покрывает ≈ ±0.19%);
- цена как контекст движения; время суток.
Глубокие уровни ±1…5% есть только в архиве — опционально (deep=True) для оценки «сколько
бы они дали», в живую модель не идут.

Каждое окно считается отдельно (sliding_window_view): значение для минуты i зависит
только от строк [i−LOOKBACK+1, i] и не меняется от длины истории — вектор по всей истории
совпадает с вектором по последнему окну бит в бит (так же будет считаться вживую).
"""

import math

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

LOOKBACK = 61  # ret_60 / rv_60: 61 цена закрытия
STEP = pd.Timedelta(minutes=1)

FEATURE_NAMES: tuple[str, ...] = (
    "flow_imb_1",
    "flow_imb_5",
    "flow_imb_15",
    "flow_imb_60",
    "flow_imb_1_z60",
    "large_imb_5",
    "large_imb_15",
    "intensity_5_60",
    "notional_5_60",
    "book_imb",
    "book_imb_5",
    "bid_chg_1",
    "ask_chg_1",
    "bid_chg_5",
    "ask_chg_5",
    "ofi_5",
    "depth_rel_60",
    "ret_1",
    "ret_5",
    "ret_15",
    "ret_60",
    "rv_15",
    "rv_60",
    "range_5",
    "hour_sin",
    "hour_cos",
)
DEEP_FEATURE_NAMES: tuple[str, ...] = ("deep_imb_1", "deep_imb_5", "deep_imb_1_chg_5")


def _wsum(x: np.ndarray, w: int) -> np.ndarray:
    """Сумма последних w значений для каждой позиции (NaN для первых w−1)."""
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1 :] = sliding_window_view(x, w).sum(axis=1)
    return out


def _wmean(x: np.ndarray, w: int) -> np.ndarray:
    return _wsum(x, w) / w


def _wstd(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1 :] = sliding_window_view(x, w).std(axis=1, ddof=1)
    return out


def _lag(x: np.ndarray, k: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    out[k:] = x[:-k]
    return out


def _ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(den > 0, num / den, np.nan)


FLOW_FEATURES = FEATURE_NAMES[:9]
DEPTH_FEATURES = FEATURE_NAMES[9:17]
CONTEXT_FEATURES = FEATURE_NAMES[17:]


def feature_set(*, depth: bool, deep: bool = False) -> tuple[str, ...]:
    """depth=False — только поток сделок + контекст (история с 2023); depth=True — плюс
    ближний стакан (с января 2026); deep — глубокие уровни (только исследование)."""
    names = FLOW_FEATURES + (DEPTH_FEATURES if depth else ()) + CONTEXT_FEATURES
    return names + (DEEP_FEATURE_NAMES if deep else ())


def compute(
    bars: pd.DataFrame, *, depth: bool = True, deep: bool = False
) -> tuple[np.ndarray, np.ndarray]:
    """Матрица признаков (n × k, порядок feature_set) и маска валидности: окно из LOOKBACK
    минут подряд, в каждой есть сделки (и ближний стакан, если depth=True)."""
    close = bars["close"].to_numpy(float)
    high = bars["high"].to_numpy(float)
    low = bars["low"].to_numpy(float)
    buy = bars["buy_notional"].to_numpy(float)
    sell = bars["sell_notional"].to_numpy(float)
    notional = buy + sell
    signed = buy - sell
    large = bars["large_buy_notional"].to_numpy(float) - bars["large_sell_notional"].to_numpy(float)
    count = (bars["buy_count"] + bars["sell_count"]).to_numpy(float)
    bid = bars["bid_near"].to_numpy(float)
    ask = bars["ask_near"].to_numpy(float)
    near_depth = bid + ask

    with np.errstate(divide="ignore", invalid="ignore"):
        logc = np.log(close)
        lr = logc - _lag(logc, 1)
        imb1 = _ratio(signed, notional)
        book = _ratio(bid - ask, near_depth)
        log_bid, log_ask = np.log(bid), np.log(ask)
        cols = [
            imb1,
            _ratio(_wsum(signed, 5), _wsum(notional, 5)),
            _ratio(_wsum(signed, 15), _wsum(notional, 15)),
            _ratio(_wsum(signed, 60), _wsum(notional, 60)),
            _ratio(imb1 - _wmean(imb1, 60), _wstd(imb1, 60)),
            _ratio(_wsum(large, 5), _wsum(notional, 5)),
            _ratio(_wsum(large, 15), _wsum(notional, 15)),
            np.log(_ratio(_wsum(count, 5) * 12, _wsum(count, 60))),
            np.log(_ratio(_wsum(notional, 5) * 12, _wsum(notional, 60))),
            book,
            _wmean(book, 5),
            log_bid - _lag(log_bid, 1),
            log_ask - _lag(log_ask, 1),
            log_bid - _lag(log_bid, 5),
            log_ask - _lag(log_ask, 5),
            (log_bid - _lag(log_bid, 5)) - (log_ask - _lag(log_ask, 5)),
            np.log(_ratio(near_depth, _wmean(near_depth, 60))),
            lr,
            logc - _lag(logc, 5),
            logc - _lag(logc, 15),
            logc - _lag(logc, 60),
            _wstd(lr, 15),
            _wstd(lr, 60),
            _ratio(
                sliding_window_max(high, 5) - sliding_window_min(low, 5),
                close,
            ),
        ]
        end = bars["minute"] + STEP
        hour = (end.dt.hour + end.dt.minute / 60).to_numpy(float) * 2 * math.pi / 24
        cols += [np.sin(hour), np.cos(hour)]
        if deep:
            dbid = bars["deep_bid_1"].to_numpy(float)
            dask = bars["deep_ask_1"].to_numpy(float)
            deep_imb = _ratio(dbid - dask, dbid + dask)
            dbid5 = bars["deep_bid_5"].to_numpy(float)
            dask5 = bars["deep_ask_5"].to_numpy(float)
            cols += [deep_imb, _ratio(dbid5 - dask5, dbid5 + dask5), deep_imb - _lag(deep_imb, 5)]

    names = FEATURE_NAMES + (DEEP_FEATURE_NAMES if deep else ())
    wanted = feature_set(depth=depth, deep=deep)
    x = np.column_stack([cols[names.index(n)] for n in wanted])
    complete = bars["trades_ok"].to_numpy(bool).copy()
    if depth:
        complete &= bars["depth_ok"].to_numpy(bool)
    minutes = bars["minute"].to_numpy("datetime64[ns]")
    consecutive = np.ones(len(bars), dtype=bool)
    consecutive[1:] = (minutes[1:] - minutes[:-1]) == np.timedelta64(60, "s")
    window_ok = _wsum((complete & consecutive).astype(float), LOOKBACK) == LOOKBACK
    valid = window_ok & np.isfinite(x).all(axis=1)
    return x, valid


def sliding_window_max(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1 :] = sliding_window_view(x, w).max(axis=1)
    return out


def sliding_window_min(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        out[w - 1 :] = sliding_window_view(x, w).min(axis=1)
    return out
