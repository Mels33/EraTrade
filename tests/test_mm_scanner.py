"""Автовыбор монет: фильтры, ранжирование, защита от частых переключений."""

from decimal import Decimal

from app.mm.scanner import MarketScan, choose_coins

D = Decimal


def m(coin: str, spread: float, volume_m: float, lev: int = 5) -> MarketScan:
    return MarketScan(
        coin=coin,
        mid=D(1),
        spread_bps=D(str(spread)),
        tick_bps=D("0.5"),
        day_volume_usd=D(str(volume_m * 1_000_000)),
        top_depth_usd=D(500),
        max_leverage=lev,
        sz_decimals=1,
    )


KW = {
    "max_coins": 2,
    "min_spread_bps": D(5),
    "max_spread_bps": D(30),
    "min_volume_usd": D(2_000_000),
    "min_leverage": 3,
    "hysteresis": 0.3,
}


def test_picks_best_by_edge_and_volume_with_filters() -> None:
    scans = [
        m("BTC", 0.2, 900),  # спред уже комиссий
        m("DEAD", 40, 3),  # слишком широкий — мёртвый рынок
        m("TINY", 9, 0.5),  # мало оборота
        m("LOWLEV", 9, 10, lev=2),  # плечо меньше нужного
        m("A", 8, 5),
        m("B", 6, 20),
        m("C", 5.5, 3),
    ]
    target, ranked = choose_coins(scans, [], **KW)  # type: ignore[arg-type]
    assert target == ["B", "A"]  # (6−3)·√20 > (8−3)·√5 > (5.5−3)·√3
    assert [s.coin for s in ranked] == ["B", "A", "C"]


def test_current_coin_kept_unless_clearly_beaten() -> None:
    scans = [m("A", 8, 5), m("B", 6, 20), m("C", 7, 5)]
    # C чуть хуже A — остаётся текущая пара B + C
    assert choose_coins(scans, ["B", "C"], **KW)[0] == ["B", "C"]  # type: ignore[arg-type]
    much_better = [m("A", 12, 10), m("B", 6, 20), m("C", 6, 3)]
    assert choose_coins(much_better, ["B", "C"], **KW)[0] == ["B", "A"]  # type: ignore[arg-type]


def test_ineligible_current_coin_is_replaced_and_no_candidates_keeps_current() -> None:
    scans = [m("A", 8, 5), m("B", 6, 20), m("C", 2, 50)]  # C: спред сузился
    assert choose_coins(scans, ["C", "B"], **KW)[0] == ["B", "A"]  # type: ignore[arg-type]
    assert choose_coins([m("C", 2, 50)], ["C", "B"], **KW)[0] == ["C", "B"]  # type: ignore[arg-type]
