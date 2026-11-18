"""Котировки маркет-мейкера: чистая логика без сети (одинакова для тестов и живой работы).

Правила для каждой монеты:
- стакан берётся без наших собственных заявок — иначе бот «догонял» бы сам себя;
- спред чужих заявок уже min_spread_bps — не котируем (комиссии съедают спред);
- спред ≥ improve_min_ticks шагов цены — встаём на шаг лучше лучшей цены, иначе присоединяемся;
- позиция: сторона, увеличивающая позицию сверх max_inventory_usd, не котируется; при заметной
  позиции увеличивающая сторона только присоединяется (не улучшает цену), а уменьшающая — улучшает;
- поток агрессивных сделок за последние flow_window_seconds: сильный перекос покупок снимает
  наш ask (нас «переедут» вверх), перекос продаж — наш bid.
"""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.config.strategy import MmConfig
from app.domain.instruments import InstrumentRules, Rounding
from app.market_data.flow import BookSnapshot, Trade

BPS = Decimal(10_000)
MIN_ORDER_USD = Decimal(10)  # биржевой минимум заявки

Levels = tuple[tuple[Decimal, Decimal], ...]


@dataclass(frozen=True, slots=True)
class Quote:
    price: Decimal
    size: Decimal
    reduce_only: bool = False


@dataclass(frozen=True, slots=True)
class QuoteDecision:
    bid: Quote | None
    ask: Quote | None
    mid: Decimal | None = None
    spread_bps: Decimal | None = None
    reasons: tuple[str, ...] = ()  # почему какая-то сторона не котируется


class FlowWindow:
    """Объём агрессивных покупок и продаж ($) за скользящее окно."""

    def __init__(self, window: timedelta) -> None:
        self.window = window
        self._items: deque[tuple[datetime, Decimal, bool]] = deque()
        self._seen: set[int] = set()
        self._seen_order: deque[int] = deque()

    def add(self, trade: Trade) -> None:
        if trade.tid in self._seen:
            return
        self._seen.add(trade.tid)
        self._seen_order.append(trade.tid)
        if len(self._seen_order) > 5000:
            self._seen.discard(self._seen_order.popleft())
        self._items.append((trade.time, trade.price * trade.size, trade.is_buy))

    def _trim(self, now: datetime) -> None:
        while self._items and self._items[0][0] < now - self.window:
            self._items.popleft()

    def volumes(self, now: datetime) -> tuple[Decimal, Decimal]:
        self._trim(now)
        buy = sum((v for _, v, b in self._items if b), Decimal(0))
        sell = sum((v for _, v, b in self._items if not b), Decimal(0))
        return buy, sell

    def imbalance(self, now: datetime, min_usd: Decimal) -> float | None:
        """(покупки − продажи) / сумма ∈ [−1, 1]; None, если оборот в окне меньше min_usd."""
        buy, sell = self.volumes(now)
        total = buy + sell
        if total <= 0 or total < min_usd:
            return None
        return float((buy - sell) / total)


def _without_own(levels: Levels, own: Quote | None) -> Levels:
    """Уровни стакана за вычетом нашей заявки (на её цене)."""
    if own is None:
        return levels
    result = []
    for price, size in levels:
        if price == own.price:
            size -= own.size
            if size <= 0:
                continue
        result.append((price, size))
    return tuple(result)


def decide(
    book: BookSnapshot,
    rules: InstrumentRules,
    cfg: MmConfig,
    *,
    position: Decimal,
    own_bid: Quote | None = None,
    own_ask: Quote | None = None,
    imbalance: float | None = None,
    reduce_only: bool = False,
) -> QuoteDecision:
    bids = _without_own(book.bids, own_bid)
    asks = _without_own(book.asks, own_ask)
    if not bids or not asks:
        return QuoteDecision(None, None, reasons=("стакан пуст",))
    best_bid, best_ask = bids[0][0], asks[0][0]
    if best_bid >= best_ask:
        return QuoteDecision(None, None, reasons=("стакан перекрещен",))
    mid = (best_bid + best_ask) / 2
    spread_bps = (best_ask - best_bid) / mid * BPS
    if spread_bps < cfg.min_spread_bps and not reduce_only:
        return QuoteDecision(None, None, mid, spread_bps, (f"спред {spread_bps:.1f} bp узкий",))

    reasons: list[str] = []
    tick = rules.tick(mid)
    spread_ticks = (best_ask - best_bid) / tick
    can_improve = spread_ticks >= cfg.improve_min_ticks
    position_usd = position * mid
    half = cfg.order_usd / 2

    bid_px = best_bid
    if can_improve and position_usd <= half:
        bid_px = rules.round_price(best_bid + tick, Rounding.DOWN)
    ask_px = best_ask
    if can_improve and position_usd >= -half:
        ask_px = rules.round_price(best_ask - tick, Rounding.UP)
    if bid_px >= ask_px:  # страховка: post-only не должен пересекать
        bid_px, ask_px = best_bid, best_ask

    if reduce_only:
        # Уходим с монеты: только заявка, закрывающая позицию (reduce-only, весь остаток).
        if position == 0:
            return QuoteDecision(None, None, mid, spread_bps, ("позиция закрыта",))
        # bid_px/ask_px уже улучшены в сторону закрытия (long → ask, short → bid).
        px = ask_px if position > 0 else bid_px
        size = abs(position)
        if size * px < MIN_ORDER_USD:
            return QuoteDecision(
                None, None, mid, spread_bps, ("остаток меньше минимальной заявки",)
            )
        q = Quote(px, size, reduce_only=True)
        return QuoteDecision(
            None if position > 0 else q,
            q if position > 0 else None,
            mid,
            spread_bps,
            ("уходим с монеты: закрываем позицию",),
        )

    want_bid = position_usd + cfg.order_usd <= cfg.max_inventory_usd
    want_ask = -position_usd + cfg.order_usd <= cfg.max_inventory_usd
    if not want_bid:
        reasons.append("bid: позиция у предела")
    if not want_ask:
        reasons.append("ask: позиция у предела")
    if imbalance is not None and imbalance >= cfg.flow_pull_imbalance and want_ask:
        want_ask = False
        reasons.append(f"ask снят: поток покупок {imbalance:+.2f}")
    if imbalance is not None and imbalance <= -cfg.flow_pull_imbalance and want_bid:
        want_bid = False
        reasons.append(f"bid снят: поток продаж {imbalance:+.2f}")

    bid = Quote(bid_px, rules.min_size_for_notional(bid_px, cfg.order_usd)) if want_bid else None
    ask = Quote(ask_px, rules.min_size_for_notional(ask_px, cfg.order_usd)) if want_ask else None
    return QuoteDecision(bid, ask, mid, spread_bps, tuple(reasons))
