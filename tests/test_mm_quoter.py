"""Котировщик маркет-мейкера: присоединение/улучшение цены, позиция, поток, свои заявки."""

from datetime import timedelta
from decimal import Decimal

from app.config.strategy import MmConfig
from app.domain.instruments import InstrumentRules
from app.market_data.flow import BookSnapshot, Trade
from app.mm.quoter import FlowWindow, Quote, decide
from tests.factories import T0

# Цена ~0.0866: 5 значащих цифр → шаг 0.00001 (≈ 1.15 bp); szDecimals 1.
RULES = InstrumentRules(coin="FART", asset_id=1, sz_decimals=1, max_price_decimals=5)
# Базовые правила (присоединение/улучшение) — без отступа и сдвига по позиции.
D = Decimal
CFG = MmConfig(coins=("FART",), min_edge_bps=D(0), inventory_skew_bps=D(0))


def book(bid: str, ask: str, *, bid_sz: str = "1000", ask_sz: str = "1000") -> BookSnapshot:
    return BookSnapshot(
        time=T0,
        bids=((D(bid), D(bid_sz)), (D(bid) - D("0.00001"), D("5000"))),
        asks=((D(ask), D(ask_sz)), (D(ask) + D("0.00001"), D("5000"))),
    )


def test_wide_spread_improves_by_one_tick_and_sizes_to_min_notional() -> None:
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(0))  # 7 шагов, ≈ 8 bp
    assert d.bid == Quote(D("0.08651"), D("138.8"))
    assert d.ask is not None and d.ask.price == D("0.08656")
    assert d.bid.price * d.bid.size >= CFG.order_usd
    assert d.ask.price * d.ask.size >= CFG.order_usd


def test_narrow_spread_in_ticks_joins_and_too_narrow_in_bp_quotes_nothing() -> None:
    cfg = MmConfig(coins=("FART",), min_spread_bps=D(2), min_edge_bps=D(0), inventory_skew_bps=D(0))
    d = decide(book("0.08650", "0.08652"), RULES, cfg, position=D(0))  # 2 шага ≈ 2.3 bp
    assert d.bid is not None and d.bid.price == D("0.08650")
    assert d.ask is not None and d.ask.price == D("0.08652")
    d = decide(book("0.08650", "0.08652"), RULES, CFG, position=D(0))  # порог 4 bp
    assert d.bid is None and d.ask is None and "узкий" in d.reasons[0]


def test_own_orders_are_excluded_from_the_book() -> None:
    # Наш bid 0.08651 — единственный на уровне: решение по чужим заявкам, цена не «уползает».
    snap = BookSnapshot(
        time=T0,
        bids=((D("0.08651"), D("138.8")), (D("0.08650"), D("1000"))),
        asks=((D("0.08657"), D("1000")),),
    )
    d = decide(snap, RULES, CFG, position=D(0), own_bid=Quote(D("0.08651"), D("138.8")))
    assert d.bid is not None and d.bid.price == D("0.08651")


def test_inventory_limit() -> None:
    long_pos = D(250)  # ≈ 21.6 USDC: ещё одна покупка превысит 25
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=long_pos)
    assert d.bid is None and any("предела" in r for r in d.reasons)
    assert d.ask is not None and d.ask.price == D("0.08656")
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(-250))
    assert d.ask is None and d.bid is not None


EDGE = MmConfig(coins=("FART",))  # отступ 3 bp и сдвиг 3 bp при полной позиции


def test_min_edge_keeps_quotes_away_from_fair_price() -> None:
    # Спред 8 bp: шаг лучше дал бы 0.08651/0.08656 (≈ 2.3 bp от середины) — ближе 3 bp, нельзя.
    d = decide(book("0.08650", "0.08657"), RULES, EDGE, position=D(0))
    assert d.bid is not None and d.ask is not None
    assert (d.bid.price, d.ask.price) == (D("0.08650"), D("0.08657"))
    mid = D("0.086535")
    assert (mid - d.bid.price) / mid * 10_000 >= 3 and (d.ask.price - mid) / mid * 10_000 >= 3
    # Спред 16 bp: можно встать на шаг лучше — отступ всё равно больше 3 bp.
    d = decide(book("0.08650", "0.08664"), RULES, EDGE, position=D(0))
    assert (d.bid.price, d.ask.price) == (D("0.08651"), D("0.08663"))  # type: ignore[union-attr]


def test_inventory_skew_shifts_quotes_against_position() -> None:
    # Спред ≈ 10 bp: отступ 3 bp ограничивает цены, сдвиг по позиции их двигает.
    b = book("0.08650", "0.08659")
    flat = decide(b, RULES, EDGE, position=D(0))
    long_ = decide(b, RULES, EDGE, position=D(140))  # ≈ +12 USDC, половина предела
    short = decide(b, RULES, EDGE, position=D(-140))
    assert flat.bid and flat.ask and long_.bid and long_.ask and short.bid and short.ask
    assert (flat.bid.price, flat.ask.price) == (D("0.08651"), D("0.08658"))
    # long: покупка уходит глубже, продажа остаётся у рынка — позицию быстрее закрывают
    assert long_.bid.price < flat.bid.price and long_.ask.price <= flat.ask.price
    # short: продажа уходит выше, покупка у рынка
    assert short.ask.price > flat.ask.price and short.bid.price >= flat.bid.price


def test_strong_flow_pulls_the_side_against_it() -> None:
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(0), imbalance=0.8)
    assert d.ask is None and d.bid is not None
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(0), imbalance=-0.7)
    assert d.bid is None and d.ask is not None
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(0), imbalance=0.3)
    assert d.bid is not None and d.ask is not None


def test_flow_window_dedups_expires_and_needs_min_volume() -> None:
    w = FlowWindow(timedelta(seconds=30))
    w.add(Trade(T0, D("0.1"), D("10000"), True, 1))  # 1000 $
    w.add(Trade(T0, D("0.1"), D("10000"), True, 1))  # дубль tid
    w.add(Trade(T0 + timedelta(seconds=10), D("0.1"), D("5000"), False, 2))  # 500 $
    now = T0 + timedelta(seconds=20)
    assert w.volumes(now) == (D(1000), D(500))
    assert w.imbalance(now, D(1000)) == 1 / 3
    assert w.imbalance(now, D(2000)) is None
    assert w.volumes(T0 + timedelta(seconds=35)) == (D(0), D(500))


def test_reduce_only_mode_quotes_only_the_closing_side() -> None:
    long_pos = D(150)  # ≈ 13 USDC long
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=long_pos, reduce_only=True)
    assert d.bid is None and d.ask == Quote(D("0.08656"), D(150), reduce_only=True)
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(-150), reduce_only=True)
    assert d.ask is None and d.bid is not None and d.bid.reduce_only and d.bid.size == D(150)
    # остаток меньше биржевого минимума — заявки нет (закроется по рынку)
    d = decide(book("0.08650", "0.08657"), RULES, CFG, position=D(50), reduce_only=True)
    assert d.bid is None and d.ask is None and "минимальной" in d.reasons[0]
    # узкий спред не мешает закрываться
    d = decide(book("0.08650", "0.08651"), RULES, CFG, position=long_pos, reduce_only=True)
    assert d.ask is not None and d.ask.price == D("0.08651")


def test_narrow_spread_keeps_only_the_closing_quote() -> None:
    narrow = book("0.08650", "0.08652")  # ≈ 2.3 bp < 4 bp
    d = decide(narrow, RULES, EDGE, position=D(0))
    assert d.bid is None and d.ask is None
    d = decide(narrow, RULES, EDGE, position=D(200))  # long ≈ 17 USDC
    assert d.bid is None and d.ask is not None
    assert d.ask.reduce_only and d.ask.size == D(200)  # закрывает ровно позицию
    assert d.ask.price >= D("0.08652")  # post-only: не пересекает стакан
    d = decide(narrow, RULES, EDGE, position=D(-200))
    assert d.ask is None and d.bid is not None and d.bid.price <= D("0.08650")
    # пыль меньше 10 USDC — заявки нет (иначе исполнение перевернуло бы позицию)
    d = decide(narrow, RULES, EDGE, position=D("-0.6"))
    assert d.bid is None and d.ask is None
