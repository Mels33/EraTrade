"""Правила стратегии без сети: цены уровней, размеры, цель, стоп (совпадают с симулятором
app/research/averaging.py: средняя цена — взвешенная размерами, как entryPx биржи)."""

from dataclasses import dataclass
from decimal import Decimal

from app.domain.instruments import InstrumentRules, Rounding

MIN_ORDER_USD = Decimal(10)


@dataclass(frozen=True, slots=True)
class CoinPlan:
    coin: str
    step: Decimal  # доля: 0.012 = 1.2%
    tp: Decimal
    levels: int
    base_usd: Decimal
    size_mult: Decimal

    def level_usd(self, k: int) -> Decimal:
        """Номинал k-й покупки (k = 0 — первая)."""
        return self.base_usd * self.size_mult**k

    def cum_usd(self, k: int) -> Decimal:
        """Номинал после k исполненных покупок."""
        return sum((self.level_usd(j) for j in range(k)), Decimal(0))


def entry_price(rules: InstrumentRules, plan: CoinPlan, mid: Decimal) -> Decimal:
    """Первая покупка — на шаг ниже текущей цены."""
    return rules.round_price(mid * (1 - plan.step), Rounding.DOWN)


def next_level_price(rules: InstrumentRules, plan: CoinPlan, last_fill: Decimal) -> Decimal:
    return rules.round_price(last_fill * (1 - plan.step), Rounding.DOWN)


def target_price(rules: InstrumentRules, plan: CoinPlan, avg_entry: Decimal) -> Decimal:
    return rules.round_price(avg_entry * (1 + plan.tp), Rounding.UP)


def stop_price(rules: InstrumentRules, plan: CoinPlan, last_fill: Decimal) -> Decimal:
    """Стоп после последнего уровня: ещё шаг ниже последней покупки."""
    return rules.round_price(last_fill * (1 - plan.step), Rounding.UP)


def level_size(rules: InstrumentRules, plan: CoinPlan, k: int, price: Decimal) -> Decimal:
    """Размер k-й покупки: номинал / цена, не меньше биржевого минимума."""
    size = rules.round_size_down(plan.level_usd(k) / price)
    if size * price < MIN_ORDER_USD:
        size = rules.min_size_for_notional(price, MIN_ORDER_USD)
    return size


def levels_filled(plan: CoinPlan, position_usd: Decimal) -> int:
    """Сколько уровней исполнено — по номиналу позиции (для восстановления после рестарта)."""
    if position_usd <= 0:
        return 0
    best = min(range(1, plan.levels + 1), key=lambda k: abs(plan.cum_usd(k) - position_usd))
    return best
