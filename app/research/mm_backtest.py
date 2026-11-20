"""Событийный бэктест маркет-мейкера по снимкам L2 и публичным сделкам Hyperliquid.

Заявка на существующем уровне встаёт за записанным объёмом. Очередь уменьшают только сделки:
исчезновение объёма из следующего снимка не считается исполнением, потому что это могла быть
отмена. Сделка по нашей цене сначала разбирает очередь, сделка сквозь цену исполняет остаток.
Это консервативная модель очереди, а не восстановление очереди по order id.
"""

import heapq
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config.strategy import MmConfig
from app.domain.account import Side
from app.domain.instruments import InstrumentRules
from app.integrations.hyperliquid.parsing import parse_ws_l2book, parse_ws_trades
from app.market_data.flow import BookSnapshot, Trade
from app.mm.quoter import FlowWindow, Quote, decide

BPS = Decimal(10_000)
Event = BookSnapshot | Trade


@dataclass(frozen=True, slots=True)
class BacktestFill:
    time: datetime
    side: Side
    price: Decimal
    size: Decimal
    queue_ahead: Decimal


@dataclass(frozen=True, slots=True)
class MarkoutSummary:
    horizon_seconds: int
    fills: int
    mean_bps: Decimal
    adverse_selection_bps: Decimal


@dataclass(frozen=True, slots=True)
class BacktestResult:
    coin: str
    start: datetime
    end: datetime
    books: int
    trades: int
    fills: int
    executions: tuple[BacktestFill, ...]
    volume_usd: Decimal
    gross_realized_usd: Decimal
    fees_usd: Decimal
    unrealized_usd: Decimal
    net_pnl_usd: Decimal
    max_drawdown_usd: Decimal
    final_position: Decimal
    markouts: tuple[MarkoutSummary, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "coin": self.coin,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "books": self.books,
            "trades": self.trades,
            "fills": self.fills,
            "volume_usd": str(self.volume_usd),
            "gross_realized_usd": str(self.gross_realized_usd),
            "fees_usd": str(self.fees_usd),
            "unrealized_usd": str(self.unrealized_usd),
            "net_pnl_usd": str(self.net_pnl_usd),
            "max_drawdown_usd": str(self.max_drawdown_usd),
            "final_position": str(self.final_position),
            "markouts": [
                {
                    "horizon_seconds": m.horizon_seconds,
                    "fills": m.fills,
                    "mean_bps": str(m.mean_bps),
                    "adverse_selection_bps": str(m.adverse_selection_bps),
                }
                for m in self.markouts
            ],
        }


@dataclass(slots=True)
class _Order:
    side: Side
    price: Decimal
    remaining: Decimal
    queue_ahead: Decimal
    active_at: datetime
    changed_at: datetime


@dataclass(slots=True)
class _PendingMarkout:
    time: datetime
    side: Side
    price: Decimal
    notional: Decimal
    values: dict[int, Decimal]


class MarketMakerBacktest:
    def __init__(
        self,
        coin: str,
        rules: InstrumentRules,
        cfg: MmConfig,
        *,
        maker_fee: Decimal = Decimal("0.00015"),
        markout_seconds: Sequence[int] = (1, 5, 30, 60),
        quote_latency_ms: int = 500,
    ) -> None:
        if maker_fee < 0 or quote_latency_ms < 0:
            raise ValueError("комиссия и задержка не могут быть отрицательными")
        if not markout_seconds or any(h <= 0 for h in markout_seconds):
            raise ValueError("горизонты markout должны быть положительными")
        self.coin = coin
        self.rules = rules
        self.cfg = cfg
        self.maker_fee = maker_fee
        self.horizons = tuple(sorted(set(markout_seconds)))
        self.latency = timedelta(milliseconds=quote_latency_ms)
        self.book: BookSnapshot | None = None
        self.position = Decimal(0)
        self.entry_price = Decimal(0)
        self.realized = Decimal(0)
        self.fees = Decimal(0)
        self.volume = Decimal(0)
        self.unrealized = Decimal(0)
        self.max_drawdown = Decimal(0)
        self.fills: list[BacktestFill] = []
        self.flow = FlowWindow(timedelta(seconds=cfg.flow_window_seconds))
        self.orders: dict[Side, _Order | None] = {Side.BUY: None, Side.SELL: None}
        self._next_quote_at: datetime | None = None
        self._last_event_at: datetime | None = None
        self._first_event_at: datetime | None = None
        self._seen_trades: set[int] = set()
        self._pending_markouts: list[_PendingMarkout] = []
        self._markouts: list[dict[int, Decimal]] = []
        self._peak_equity = Decimal(0)
        self._books = 0
        self._trades = 0

    def run(self, events: Iterable[Event]) -> BacktestResult:
        for event in events:
            self.process(event)
        if self._first_event_at is None or self._last_event_at is None:
            raise ValueError("нет рыночных событий")
        return BacktestResult(
            coin=self.coin,
            start=self._first_event_at,
            end=self._last_event_at,
            books=self._books,
            trades=self._trades,
            fills=len(self.fills),
            executions=tuple(self.fills),
            volume_usd=self.volume,
            gross_realized_usd=self.realized,
            fees_usd=self.fees,
            unrealized_usd=self.unrealized,
            net_pnl_usd=self.realized - self.fees + self.unrealized,
            max_drawdown_usd=self.max_drawdown,
            final_position=self.position,
            markouts=self._markout_summaries(),
        )

    def process(self, event: Event) -> None:
        if self._last_event_at is not None and event.time < self._last_event_at:
            raise ValueError("события должны быть отсортированы по времени")
        self._first_event_at = self._first_event_at or event.time
        self._last_event_at = event.time
        if isinstance(event, BookSnapshot):
            self.book = event
            self._books += 1
            self._mark(event.time)
        elif event.tid not in self._seen_trades:
            self._seen_trades.add(event.tid)
            self._trades += 1
            self.flow.add(event)
            self._execute(event)
            self._mark(event.time)
        self._requote(event.time)

    def _mark(self, now: datetime) -> None:
        if self.book is None or not self.book.bids or not self.book.asks:
            return
        mid = (self.book.bids[0][0] + self.book.asks[0][0]) / 2
        self.unrealized = self.position * (mid - self.entry_price) if self.position else Decimal(0)
        equity = self.realized - self.fees + self.unrealized
        self._peak_equity = max(self._peak_equity, equity)
        self.max_drawdown = min(self.max_drawdown, equity - self._peak_equity)
        pending: list[_PendingMarkout] = []
        for item in self._pending_markouts:
            for horizon in self.horizons:
                if horizon not in item.values and now >= item.time + timedelta(seconds=horizon):
                    sign = Decimal(1 if item.side is Side.BUY else -1)
                    item.values[horizon] = sign * (mid / item.price - 1) * BPS
            if len(item.values) == len(self.horizons):
                self._markouts.append(item.values)
            else:
                pending.append(item)
        self._pending_markouts = pending

    def _requote(self, now: datetime) -> None:
        if self.book is None or (self._next_quote_at is not None and now < self._next_quote_at):
            return
        imbalance = self.flow.imbalance(now, self.cfg.flow_min_usd)
        decision = decide(
            self.book,
            self.rules,
            self.cfg,
            position=self.position,
            imbalance=imbalance,
        )
        for side, quote in ((Side.BUY, decision.bid), (Side.SELL, decision.ask)):
            current = self.orders[side]
            if quote is None:
                self.orders[side] = None
            elif current is None:
                self._place(side, quote, now)
            else:
                tolerance = max(
                    self.rules.tick(quote.price), quote.price * self.cfg.requote_slack_bps / BPS
                )
                retreat = (
                    quote.price < current.price if side is Side.BUY else quote.price > current.price
                )
                interval = timedelta(seconds=self.cfg.requote_min_interval_seconds)
                if abs(current.price - quote.price) > tolerance and (
                    retreat or now >= current.changed_at + interval
                ):
                    self._place(side, quote, now)
        self._next_quote_at = now + self.latency

    def _place(self, side: Side, quote: Quote, now: datetime) -> None:
        if self.book is None:
            raise RuntimeError("заявка без стакана")
        levels = self.book.bids if side is Side.BUY else self.book.asks
        ahead = next((size for price, size in levels if price == quote.price), Decimal(0))
        self.orders[side] = _Order(
            side=side,
            price=quote.price,
            remaining=quote.size,
            queue_ahead=ahead,
            active_at=now + self.latency,
            changed_at=now,
        )

    def _execute(self, trade: Trade) -> None:
        side = Side.BUY if not trade.is_buy else Side.SELL
        order = self.orders[side]
        if order is None or trade.time < order.active_at:
            return
        through = trade.price < order.price if side is Side.BUY else trade.price > order.price
        at_price = trade.price == order.price
        if not through and not at_price:
            return
        if through:
            size = order.remaining
            ahead = order.queue_ahead
            order.queue_ahead = Decimal(0)
        else:
            ahead = order.queue_ahead
            executable = max(Decimal(0), trade.size - order.queue_ahead)
            order.queue_ahead = max(Decimal(0), order.queue_ahead - trade.size)
            size = min(order.remaining, executable)
        if size <= 0:
            return
        self._fill(order, size, ahead, trade.time)
        order.remaining -= size
        if order.remaining <= 0:
            self.orders[side] = None

    def _fill(self, order: _Order, size: Decimal, ahead: Decimal, now: datetime) -> None:
        sign = Decimal(1 if order.side is Side.BUY else -1)
        delta = sign * size
        old = self.position
        if old == 0 or old * delta > 0:
            total = abs(old) + size
            self.entry_price = (abs(old) * self.entry_price + size * order.price) / total
        else:
            closing = min(abs(old), size)
            old_sign = Decimal(1 if old > 0 else -1)
            self.realized += closing * (order.price - self.entry_price) * old_sign
            if size > abs(old):
                self.entry_price = order.price
            elif size == abs(old):
                self.entry_price = Decimal(0)
        self.position += delta
        notional = size * order.price
        fee = notional * self.maker_fee
        self.fees += fee
        self.volume += notional
        self.fills.append(BacktestFill(now, order.side, order.price, size, ahead))
        self._pending_markouts.append(_PendingMarkout(now, order.side, order.price, notional, {}))

    def _markout_summaries(self) -> tuple[MarkoutSummary, ...]:
        result = []
        measured = self._markouts + [item.values for item in self._pending_markouts]
        for horizon in self.horizons:
            values = [m[horizon] for m in measured if horizon in m]
            if not values:
                result.append(MarkoutSummary(horizon, 0, Decimal(0), Decimal(0)))
                continue
            mean = sum(values, Decimal(0)) / len(values)
            result.append(MarkoutSummary(horizon, len(values), mean, max(Decimal(0), -mean)))
        return tuple(result)


def _rows(raw: str) -> list[Any]:
    value = json.loads(raw)
    if isinstance(value, list):
        return value
    if not isinstance(value, dict):
        raise ValueError("строка рыночных данных должна быть JSON-объектом или массивом")
    if "data" in value:
        data = value["data"]
        return data if isinstance(data, list) else [data]
    if "fills" in value:
        fills = value["fills"]
        if not isinstance(fills, list):
            raise ValueError("fills должен быть массивом")
        return fills
    return [value]


def read_book(path: Path, coin: str) -> Iterator[BookSnapshot]:
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            for row in _rows(line):
                try:
                    book = parse_ws_l2book(row, coin=coin)
                except Exception as exc:
                    raise ValueError(f"{path}:{line_number}: некорректный l2Book") from exc
                if book is not None:
                    yield book


def read_trades(path: Path, coin: str) -> Iterator[Trade]:
    with path.open(encoding="utf-8") as f:
        for line_number, line in enumerate(f, 1):
            if not line.strip():
                continue
            for row in _rows(line):
                rows = row if isinstance(row, list) else [row]
                try:
                    trades = parse_ws_trades(rows, coin=coin)
                except Exception as exc:
                    raise ValueError(f"{path}:{line_number}: некорректная сделка") from exc
                yield from trades


def read_events(
    book_paths: Sequence[Path], trade_paths: Sequence[Path], coin: str
) -> Iterator[Event]:
    iterators: list[Iterator[Event]] = [read_book(path, coin) for path in book_paths]
    iterators += [read_trades(path, coin) for path in trade_paths]
    yield from heapq.merge(*iterators, key=lambda event: event.time)


def run_files(
    coin: str,
    book_paths: Sequence[Path],
    trade_paths: Sequence[Path],
    rules: InstrumentRules,
    cfg: MmConfig,
    **kwargs: Any,
) -> BacktestResult:
    backtest = MarketMakerBacktest(coin, rules, cfg, **kwargs)
    return backtest.run(read_events(book_paths, trade_paths, coin))
