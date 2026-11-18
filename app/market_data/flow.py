"""Поток сделок и стакан Hyperliquid, агрегированные по корзинам (по умолчанию 30 минут).

Истории этих данных у биржи нет — их копит коллектор с момента запуска. Корзина
считается полной, только если сбор шёл всё её время без обрывов WebSocket; неполная
корзина сохраняется с complete=false, и признаки по ней не строятся (решение — HOLD).

Поток: сторона агрессора из поля side сделки (B — покупка по рынку, A — продажа),
дедупликация по tid (при подписке и переподключении биржа присылает последние сделки).
Стакан: снимки не чаще раза в sample_seconds; из каждого — спред, дисбаланс объёма на
5 и 20 уровнях, глубина 20 уровней в долларах.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def bucket_start(t: datetime, step: timedelta) -> datetime:
    return EPOCH + ((t - EPOCH) // step) * step


@dataclass(frozen=True, slots=True)
class Trade:
    time: datetime
    price: Decimal
    size: Decimal
    is_buy: bool  # агрессор купил
    tid: int


@dataclass(frozen=True, slots=True)
class BookSnapshot:
    time: datetime
    bids: tuple[tuple[Decimal, Decimal], ...]  # (цена, размер), лучшие первыми
    asks: tuple[tuple[Decimal, Decimal], ...]


@dataclass
class FlowBucket:
    open_time: datetime
    complete: bool
    buy_volume: Decimal = Decimal(0)
    sell_volume: Decimal = Decimal(0)
    buy_count: int = 0
    sell_count: int = 0
    buy_notional: Decimal = Decimal(0)
    sell_notional: Decimal = Decimal(0)
    large_buy_notional: Decimal = Decimal(0)
    large_sell_notional: Decimal = Decimal(0)
    tids: set[int] = field(default_factory=set, repr=False)


@dataclass
class BookBucket:
    open_time: datetime
    complete: bool
    samples: int = 0
    spread_bps_sum: float = 0.0
    imbalance5_sum: float = 0.0
    imbalance20_sum: float = 0.0
    depth20_usd_sum: float = 0.0
    last_imbalance5: float | None = None

    def mean(self, total: float) -> float | None:
        return total / self.samples if self.samples else None


def _imbalance(bids: float, asks: float) -> float:
    return (bids - asks) / (bids + asks) if bids + asks > 0 else 0.0


class FlowAggregator:
    """Корзины потока сделок. accept_from — момент начала сбора: сделки раньше него
    (снимок при подписке) не учитываются, корзина начала сбора — неполная."""

    def __init__(
        self, step: timedelta, *, started_at: datetime, large_notional: Decimal = Decimal(100_000)
    ) -> None:
        self.step = step
        self.accept_from = started_at
        self.large_notional = large_notional
        self._buckets: dict[datetime, FlowBucket] = {}
        self._gap_buckets: set[datetime] = set()

    def _bucket(self, t: datetime) -> FlowBucket:
        start = bucket_start(t, self.step)
        bucket = self._buckets.get(start)
        if bucket is None:
            complete = start >= self.accept_from and start not in self._gap_buckets
            bucket = self._buckets[start] = FlowBucket(start, complete)
        return bucket

    def add(self, trades: list[Trade]) -> int:
        added = 0
        for t in trades:
            if t.time < self.accept_from:
                continue
            bucket = self._bucket(t.time)
            if t.tid in bucket.tids:
                continue
            bucket.tids.add(t.tid)
            notional = t.price * t.size
            if t.is_buy:
                bucket.buy_volume += t.size
                bucket.buy_count += 1
                bucket.buy_notional += notional
                if notional >= self.large_notional:
                    bucket.large_buy_notional += notional
            else:
                bucket.sell_volume += t.size
                bucket.sell_count += 1
                bucket.sell_notional += notional
                if notional >= self.large_notional:
                    bucket.large_sell_notional += notional
            added += 1
        return added

    def mark_gap(self, since: datetime, until: datetime) -> None:
        """Обрыв сбора: все корзины, задетые промежутком [since, until], неполные."""
        start = bucket_start(since, self.step)
        while start <= until:
            self._gap_buckets.add(start)
            if start in self._buckets:
                self._buckets[start].complete = False
            start += self.step

    def pop_closed(
        self, now: datetime, grace: timedelta = timedelta(seconds=5)
    ) -> list[FlowBucket]:
        closed = [b for s, b in self._buckets.items() if s + self.step + grace <= now]
        for b in closed:
            del self._buckets[b.open_time]
            self._gap_buckets.discard(b.open_time)
        return sorted(closed, key=lambda b: b.open_time)


class BookAggregator:
    def __init__(
        self, step: timedelta, *, started_at: datetime, sample_seconds: float = 10.0
    ) -> None:
        self.step = step
        self.accept_from = started_at
        self.sample = timedelta(seconds=sample_seconds)
        self._last_sample: datetime | None = None
        self._buckets: dict[datetime, BookBucket] = {}
        self._gap_buckets: set[datetime] = set()

    def add(self, snap: BookSnapshot) -> bool:
        if snap.time < self.accept_from or not snap.bids or not snap.asks:
            return False
        if self._last_sample is not None and snap.time - self._last_sample < self.sample:
            return False
        self._last_sample = snap.time
        start = bucket_start(snap.time, self.step)
        bucket = self._buckets.get(start)
        if bucket is None:
            complete = start >= self.accept_from and start not in self._gap_buckets
            bucket = self._buckets[start] = BookBucket(start, complete)
        best_bid, best_ask = snap.bids[0][0], snap.asks[0][0]
        mid = (best_bid + best_ask) / 2
        bid5 = float(sum(sz for _, sz in snap.bids[:5]))
        ask5 = float(sum(sz for _, sz in snap.asks[:5]))
        bid20 = float(sum(sz for _, sz in snap.bids[:20]))
        ask20 = float(sum(sz for _, sz in snap.asks[:20]))
        depth = float(sum(px * sz for px, sz in (*snap.bids[:20], *snap.asks[:20])))
        bucket.samples += 1
        bucket.spread_bps_sum += float((best_ask - best_bid) / mid * 10_000)
        imb5 = _imbalance(bid5, ask5)
        bucket.imbalance5_sum += imb5
        bucket.imbalance20_sum += _imbalance(bid20, ask20)
        bucket.depth20_usd_sum += depth
        bucket.last_imbalance5 = imb5
        return True

    def mark_gap(self, since: datetime, until: datetime) -> None:
        start = bucket_start(since, self.step)
        while start <= until:
            self._gap_buckets.add(start)
            if start in self._buckets:
                self._buckets[start].complete = False
            start += self.step

    def pop_closed(
        self, now: datetime, grace: timedelta = timedelta(seconds=5)
    ) -> list[BookBucket]:
        closed = [b for s, b in self._buckets.items() if s + self.step + grace <= now]
        for b in closed:
            del self._buckets[b.open_time]
            self._gap_buckets.discard(b.open_time)
        return sorted(closed, key=lambda b: b.open_time)
