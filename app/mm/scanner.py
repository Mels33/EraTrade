"""Выбор рынков для маркет-мейкинга: спред шире комиссии maker, достаточный оборот.

Комиссия maker 1.5 bp с каждой стороны: круг «купил по bid — продал по ask» окупается,
только если спред заметно шире 3 bp. На BTC/ETH спред 0.1–0.5 bp — минус по построению.
"""

import asyncio
import math
from dataclasses import dataclass
from decimal import Decimal
from statistics import median

from app.domain.instruments import InstrumentRules
from app.integrations.hyperliquid.gateway import HyperliquidGateway
from app.integrations.hyperliquid.parsing import parse_ws_l2book

BPS = Decimal(10_000)


@dataclass(frozen=True, slots=True)
class MarketScan:
    coin: str
    mid: Decimal
    spread_bps: Decimal  # медиана по замерам
    tick_bps: Decimal  # шаг цены в bp: спред не бывает уже одного шага
    day_volume_usd: Decimal
    top_depth_usd: Decimal  # медиана min(объём на лучшем bid, на лучшем ask) в $
    max_leverage: int
    sz_decimals: int

    @property
    def edge_bps(self) -> Decimal:
        """Спред минус комиссии круга maker (2 × 1.5 bp)."""
        return self.spread_bps - 3


async def scan(
    gateway: HyperliquidGateway,
    *,
    top: int = 40,
    samples: int = 3,
    pause_s: float = 2.0,
    min_volume_usd: Decimal = Decimal(1_000_000),
) -> list[MarketScan]:
    """Топ рынков по обороту за сутки; несколько замеров стакана на каждый."""
    markets, contexts = await gateway.get_markets_with_contexts()
    eligible = [
        m
        for m in markets
        if not m.is_delisted
        and (ctx := contexts.get(m.coin)) is not None
        and (ctx.day_notional_volume or 0) >= min_volume_usd
    ]
    eligible.sort(key=lambda m: contexts[m.coin].day_notional_volume or 0, reverse=True)
    eligible = eligible[:top]

    spreads: dict[str, list[Decimal]] = {m.coin: [] for m in eligible}
    depths: dict[str, list[Decimal]] = {m.coin: [] for m in eligible}
    mids: dict[str, Decimal] = {}
    for k in range(samples):
        if k:
            await asyncio.sleep(pause_s)
        for m in eligible:
            raw = await gateway._client.post({"type": "l2Book", "coin": m.coin})
            book = parse_ws_l2book(raw, coin=m.coin)
            if book is None or not book.bids or not book.asks:
                continue
            (bid, bid_sz), (ask, ask_sz) = book.bids[0], book.asks[0]
            mid = (bid + ask) / 2
            mids[m.coin] = mid
            spreads[m.coin].append((ask - bid) / mid * BPS)
            depths[m.coin].append(min(bid * bid_sz, ask * ask_sz))

    result = []
    for m in eligible:
        if not spreads[m.coin]:
            continue
        mid = mids[m.coin]
        rules = InstrumentRules.for_perp(m)
        result.append(
            MarketScan(
                coin=m.coin,
                mid=mid,
                spread_bps=Decimal(median(spreads[m.coin])),
                tick_bps=rules.tick(mid) / mid * BPS,
                day_volume_usd=contexts[m.coin].day_notional_volume or Decimal(0),
                top_depth_usd=Decimal(median(depths[m.coin])),
                max_leverage=m.max_leverage,
                sz_decimals=m.sz_decimals,
            )
        )
    return result


def candidates(
    scans: list[MarketScan],
    *,
    min_spread_bps: Decimal = Decimal(4),
    max_spread_bps: Decimal = Decimal(30),
    min_volume_usd: Decimal = Decimal(1_000_000),
) -> list[MarketScan]:
    """Подходящие рынки, лучшие первыми: спред шире комиссий, но не «мёртвый» рынок.

    Слишком широкий спред (> max_spread_bps) обычно означает редкие сделки и резкие скачки
    цены — там котировку чаще «сбивают» информированные участники.
    """
    good = [
        s
        for s in scans
        if min_spread_bps <= s.spread_bps <= max_spread_bps and s.day_volume_usd >= min_volume_usd
    ]
    # Ожидаемый заработок ~ (спред − комиссии) × оборот; оборот — под корнем, чтобы не
    # выбирать только самые ликвидные рынки с минимальным запасом по спреду.
    return sorted(good, key=lambda s: s.edge_bps * s.day_volume_usd.sqrt(), reverse=True)


def score(s: MarketScan) -> float:
    """Ожидаемый заработок ~ (спред − комиссии) × √оборот."""
    return float(s.edge_bps) * math.sqrt(float(s.day_volume_usd))


def choose_coins(
    scans: list[MarketScan],
    current: list[str],
    *,
    max_coins: int,
    min_spread_bps: Decimal,
    max_spread_bps: Decimal,
    min_volume_usd: Decimal,
    min_leverage: int,
    exclude: tuple[str, ...] = (),
    hysteresis: float = 0.3,
) -> tuple[list[str], list[MarketScan]]:
    """Монеты для маркет-мейкинга и ранжированные кандидаты.

    Текущая монета остаётся, пока подходит под фильтры; вытесняет её только кандидат, который
    лучше на hysteresis (защита от частых переключений). Нет ни одного кандидата — остаёмся
    на текущих (узкий спред бот и так пережидает, не котируя).
    """
    good = [
        s
        for s in candidates(
            scans,
            min_spread_bps=min_spread_bps,
            max_spread_bps=max_spread_bps,
            min_volume_usd=min_volume_usd,
        )
        if s.max_leverage >= min_leverage and s.coin not in exclude
    ]
    ranked = sorted(good, key=score, reverse=True)
    if not ranked:
        return list(current)[:max_coins], ranked
    by_coin = {s.coin: s for s in ranked}
    target = [c for c in current if c in by_coin][:max_coins]
    for s in ranked:
        if len(target) >= max_coins:
            break
        if s.coin not in target:
            target.append(s.coin)
    outsiders = [s for s in ranked if s.coin not in target]
    while outsiders and target:
        weakest = min(target, key=lambda c: score(by_coin[c]))
        if score(outsiders[0]) <= score(by_coin[weakest]) * (1 + hysteresis):
            break
        target[target.index(weakest)] = outsiders.pop(0).coin
    return target, ranked
