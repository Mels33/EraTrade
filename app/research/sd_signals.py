"""Сигналы «спрос и предложение» на часовых свечах. Только прошлое: сигнал часа i использует
данные часов ≤ i (проверяется тестом на усечённых данных).

A. Зоны «база → импульс»: узкая база base часов (диапазон ≤ ATR), затем в течение 6 часов
   закрытие ушло на ≥ k × ATR от базы. Спрос — база перед ростом: лимит на покупку у верхней
   границы базы при возврате; предложение — зеркально. Стоп за дальней границей + 0.25 ATR;
   цель — 2R или ближайший узел объёма (HVN) профиля за 14 дней по ходу сделки.
B. Стены в стакане: z-оценка log(глубина bid / глубина ask) в полосе (0–1%, 1–2%, 0–5%)
   относительно 30 дней; пересечение |z| ≥ 2 — вход по рынку в сторону толстой стороны
   (follow) или против (fade).
C. Дисбаланс за дни: (покупки − продажи) / объём за 1/3/7 дней, z относительно 90 дней;
   пересечение |z| ≥ 1.5 — вход по рынку по дельте (follow) или против (fade).
"""

from dataclasses import dataclass
from typing import Literal

import numpy as np
import pandas as pd

from app.research.hourly_sim import Signal

ATR_HOURS = 24
IMPULSE_WINDOW = 6
PROFILE_HOURS = 14 * 24
PROFILE_BIN = 0.0025  # корзина профиля объёма — 0.25% цены


def atr(h: pd.DataFrame, hours: int = ATR_HOURS) -> np.ndarray:
    prev = h["close"].shift(1)
    tr = pd.concat(
        [h["high"] - h["low"], (h["high"] - prev).abs(), (h["low"] - prev).abs()], axis=1
    ).max(axis=1)
    result: np.ndarray = tr.rolling(hours).mean().to_numpy()
    return result


def _zscore(x: pd.Series, window: int) -> np.ndarray:
    mean = x.rolling(window, min_periods=window // 2).mean()
    std = x.rolling(window, min_periods=window // 2).std()
    z: np.ndarray = ((x - mean) / std).to_numpy()
    return z


def _crossings(z: np.ndarray, thr: float) -> list[tuple[int, int]]:
    """(час, знак): z впервые вышел за ±thr (предыдущий час был внутри)."""
    out = []
    for i in range(1, len(z)):
        a, b = z[i - 1], z[i]
        if np.isnan(a) or np.isnan(b):
            continue
        if b >= thr > a:
            out.append((i, 1))
        elif b <= -thr < a:
            out.append((i, -1))
    return out


# --- A. зоны ---


@dataclass(frozen=True, slots=True)
class ZoneParams:
    base: int = 2  # часов в базе
    k: float = 2.0  # импульс в ATR
    hold: int = 48
    target: Literal["2r", "hvn"] = "2r"


def hvn_levels(h: pd.DataFrame, i: int) -> np.ndarray:
    """Узлы большого объёма профиля за 14 дней до часа i включительно (цены корзин)."""
    lo = max(0, i - PROFILE_HOURS + 1)
    vwap = h["vwap"].to_numpy()[lo : i + 1]
    vol = h["volume"].to_numpy()[lo : i + 1]
    if len(vwap) < 24:
        return np.array([])
    keys = np.floor(np.log(vwap) / np.log1p(PROFILE_BIN)).astype(np.int64)
    uniq, inv = np.unique(keys, return_inverse=True)
    sums = np.bincount(inv, weights=vol)
    # локальные максимумы, заметно выше среднего
    peaks = [
        j
        for j in range(len(sums))
        if sums[j] >= 1.5 * sums.mean()
        and (j == 0 or sums[j] >= sums[j - 1])
        and (j == len(sums) - 1 or sums[j] >= sums[j + 1])
    ]
    levels: np.ndarray = np.exp((uniq[peaks] + 0.5) * np.log1p(PROFILE_BIN))
    return levels


def zone_signals(h: pd.DataFrame, p: ZoneParams) -> list[Signal]:
    a = atr(h)
    high, low, close = h["high"].to_numpy(), h["low"].to_numpy(), h["close"].to_numpy()
    roll_hi = h["high"].rolling(p.base).max().to_numpy()
    roll_lo = h["low"].rolling(p.base).min().to_numpy()
    signals: list[Signal] = []
    used: set[int] = set()  # час обнаружения — одна зона на час
    for b in range(len(h)):
        if np.isnan(a[b]) or np.isnan(roll_hi[b]) or roll_hi[b] - roll_lo[b] > a[b]:
            continue
        top, bottom = roll_hi[b], roll_lo[b]
        run_lo, run_hi = np.inf, -np.inf
        for t in range(b + 1, min(b + 1 + IMPULSE_WINDOW, len(h))):
            run_lo, run_hi = min(run_lo, low[t]), max(run_hi, high[t])
            if t in used:
                break
            # импульс вверх, не заходивший ниже базы, — зона спроса
            if close[t] >= top + p.k * a[b] and run_lo >= bottom:
                used.add(t)
                signals.append(_zone_signal(h, t, "long", top, bottom - 0.25 * a[b], p))
                break
            # импульс вниз, не заходивший выше базы, — зона предложения
            if close[t] <= bottom - p.k * a[b] and run_hi <= top:
                used.add(t)
                signals.append(_zone_signal(h, t, "short", bottom, top + 0.25 * a[b], p))
                break
    return signals


def _zone_signal(
    h: pd.DataFrame,
    t: int,
    side: Literal["long", "short"],
    entry: float,
    stop: float,
    p: ZoneParams,
) -> Signal:
    risk = abs(entry - stop)
    target = entry + 2 * risk if side == "long" else entry - 2 * risk
    if p.target == "hvn":
        levels = hvn_levels(h, t)
        if side == "long":
            ahead = levels[levels >= entry + risk]
            target = float(ahead.min()) if len(ahead) else target
        else:
            ahead = levels[levels <= entry - risk]
            target = float(ahead.max()) if len(ahead) else target
    return Signal(
        i=t, side=side, entry="limit", price=entry, stop=stop, target=target, max_hours=p.hold
    )


# --- B. стены в стакане ---


@dataclass(frozen=True, slots=True)
class WallParams:
    band: Literal["1", "2", "5"] = "1"
    hold: int = 24
    direction: Literal["follow", "fade"] = "follow"
    thr: float = 2.0


def book_asymmetry(h: pd.DataFrame, band: str) -> pd.Series:
    if band == "2":  # полоса 1–2%: прирост глубины между ±1% и ±2%
        bid = h["deep_bid_2"] - h["deep_bid_1"]
        ask = h["deep_ask_2"] - h["deep_ask_1"]
    else:
        bid, ask = h[f"deep_bid_{band}"], h[f"deep_ask_{band}"]
    x = np.log(bid.clip(lower=1) / ask.clip(lower=1))
    return x.where(h["depth_ok"])


def wall_signals(h: pd.DataFrame, p: WallParams) -> list[Signal]:
    z = _zscore(book_asymmetry(h, p.band), 30 * 24)
    return _market_signals(h, _crossings(z, p.thr), p.direction, p.hold, stop_atr=1.5, tp_atr=3.0)


# --- C. дисбаланс за дни ---


@dataclass(frozen=True, slots=True)
class ImbalanceParams:
    days: int = 3
    hold: int = 24
    direction: Literal["follow", "fade"] = "follow"
    thr: float = 1.5


def delta_ratio(h: pd.DataFrame, days: int) -> pd.Series:
    w = days * 24
    delta = (h["buy"] - h["sell"]).rolling(w).sum()
    return delta / h["volume"].rolling(w).sum()


def imbalance_signals(h: pd.DataFrame, p: ImbalanceParams) -> list[Signal]:
    z = _zscore(delta_ratio(h, p.days), 90 * 24)
    return _market_signals(h, _crossings(z, p.thr), p.direction, p.hold, stop_atr=2.0, tp_atr=None)


def _market_signals(
    h: pd.DataFrame,
    crossings: list[tuple[int, int]],
    direction: str,
    hold: int,
    *,
    stop_atr: float,
    tp_atr: float | None,
) -> list[Signal]:
    a = atr(h)
    close = h["close"].to_numpy()
    out = []
    for i, sgn in crossings:
        if np.isnan(a[i]):
            continue
        s = sgn if direction == "follow" else -sgn
        side: Literal["long", "short"] = "long" if s > 0 else "short"
        ref = close[i]  # стоп/цель — от закрытия часа сигнала (вход — open следующего)
        out.append(
            Signal(
                i=i,
                side=side,
                entry="market",
                stop=ref - s * stop_atr * a[i],
                target=None if tp_atr is None else ref + s * tp_atr * a[i],
                max_hours=hold,
            )
        )
    return out
