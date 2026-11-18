"""Правила точности цены и размера Hyperliquid (Tick and lot size).

- Размер: не больше szDecimals знаков после запятой.
- Цена perp: не больше 5 значащих цифр и не больше (6 - szDecimals) знаков после запятой.
  Целая цена допустима всегда, независимо от числа значащих цифр.

Округление всегда выполняется в явно заданную сторону, чтобы не ослабить риск:
- размер позиции — вниз (round_size_down);
- лимитная цена покупки (верхняя граница) — вниз, продажи (нижняя граница) — вверх;
- защитный stop — в сторону цены входа (stop long — вверх, stop short — вниз).
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from app.domain.market import MarketMeta

PERP_MAX_DECIMALS = 6
MAX_SIGNIFICANT_FIGURES = 5


class Rounding(StrEnum):
    UP = "up"
    DOWN = "down"

    @property
    def decimal_mode(self) -> str:
        return ROUND_CEILING if self is Rounding.UP else ROUND_FLOOR


class PrecisionError(ValueError):
    """Значение нельзя привести к допустимому для биржи."""


def _quantum(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def _decimal_places(value: Decimal) -> int:
    exponent = value.normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # NaN / Infinity
        raise PrecisionError(f"не конечное число: {value}")
    return max(0, -exponent)


def _significant_figures(value: Decimal) -> int:
    return len(value.normalize().as_tuple().digits)


def to_wire(value: Decimal) -> str:
    """Строка для API: без экспоненты и без хвостовых нулей ('100', '0.001', '65000.5')."""
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


@dataclass(frozen=True, slots=True)
class InstrumentRules:
    coin: str
    asset_id: int
    sz_decimals: int
    max_price_decimals: int

    @classmethod
    def for_perp(cls, meta: MarketMeta) -> "InstrumentRules":
        return cls(
            coin=meta.coin,
            asset_id=meta.asset_id,
            sz_decimals=meta.sz_decimals,
            max_price_decimals=PERP_MAX_DECIMALS - meta.sz_decimals,
        )

    # --- размер ---

    def round_size_down(self, size: Decimal) -> Decimal:
        if size < 0:
            raise PrecisionError("размер не может быть отрицательным")
        return size.quantize(_quantum(self.sz_decimals), rounding=ROUND_FLOOR)

    def is_valid_size(self, size: Decimal) -> bool:
        return size > 0 and _decimal_places(size) <= self.sz_decimals

    # --- цена ---

    def tick(self, price: Decimal) -> Decimal:
        """Шаг цены около price: более грубое из двух ограничений; шаг крупнее 1 не нужен —
        целые цены разрешены."""
        if price <= 0:
            raise PrecisionError("цена должна быть положительной")
        decimals_quantum = _quantum(self.max_price_decimals)
        sig_quantum = Decimal(1).scaleb(price.adjusted() - (MAX_SIGNIFICANT_FIGURES - 1))
        return min(max(decimals_quantum, sig_quantum), Decimal(1))

    def round_price(self, price: Decimal, rounding: Rounding) -> Decimal:
        if price <= 0:
            raise PrecisionError("цена должна быть положительной")
        result = price.quantize(self.tick(price), rounding=rounding.decimal_mode)
        if result <= 0:
            raise PrecisionError(f"цена {price} слишком мала для {self.coin}")
        # Перенос разряда при округлении вверх (99999.5 → 100000) даёт только хвостовые нули,
        # поэтому результат всегда допустим; проверка — страховка от ошибки в алгоритме.
        if not self.is_valid_price(result):
            raise PrecisionError(f"внутренняя ошибка округления: {price} → {result}")
        return result

    def is_valid_price(self, price: Decimal) -> bool:
        if price <= 0:
            return False
        if _decimal_places(price) > self.max_price_decimals:
            return False
        if price == price.to_integral_value():
            return True
        return _significant_figures(price) <= MAX_SIGNIFICANT_FIGURES

    # --- номинал ---

    @staticmethod
    def notional(size: Decimal, price: Decimal) -> Decimal:
        return abs(size) * price

    def meets_min_notional(self, size: Decimal, price: Decimal, min_notional: Decimal) -> bool:
        return self.notional(size, price) >= min_notional

    def min_size_for_notional(self, price: Decimal, min_notional: Decimal) -> Decimal:
        """Наименьший допустимый размер с номиналом не ниже min_notional (округление вверх).

        Не для расчёта позиции — её размер всегда округляется вниз. Используется, чтобы понять,
        дотягивает ли рассчитанный от риска размер до биржевого минимума.
        """
        if price <= 0 or min_notional <= 0:
            raise PrecisionError("цена и минимальный номинал должны быть положительными")
        return (min_notional / price).quantize(_quantum(self.sz_decimals), rounding=ROUND_CEILING)
