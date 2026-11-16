from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.domain.instruments import InstrumentRules, PrecisionError, Rounding, to_wire
from tests.factories import market_meta

UP, DOWN = Rounding.UP, Rounding.DOWN


def rules(sz_decimals: int) -> InstrumentRules:
    return InstrumentRules.for_perp(market_meta(sz_decimals=sz_decimals))


BTC = rules(5)  # реальный szDecimals BTC: цена до 1 знака после запятой


# --- примеры из документации Hyperliquid (Tick and lot size) ---


@pytest.mark.parametrize(
    ("sz_decimals", "price", "valid"),
    [
        (0, "1234.5", True),
        (0, "1234.56", False),  # 6 значащих цифр
        (0, "0.001234", True),
        (0, "0.0012345", False),  # больше 6 знаков после запятой
        (1, "0.01234", True),
        (1, "0.012345", False),  # больше 6 - 1 знаков
        (0, "123456", True),  # целая цена допустима всегда
        (0, "12345.6", False),
    ],
)
def test_documented_price_examples(sz_decimals: int, price: str, valid: bool) -> None:
    assert rules(sz_decimals).is_valid_price(Decimal(price)) is valid


def test_documented_size_example() -> None:
    r = rules(3)
    assert r.is_valid_size(Decimal("1.001"))
    assert not r.is_valid_size(Decimal("1.0001"))


# --- BTC ---


@pytest.mark.parametrize(
    ("price", "rounding", "expected"),
    [
        ("64974.37", DOWN, "64974"),  # 5 значащих цифр → целое
        ("64974.37", UP, "64975"),
        ("64974", UP, "64974"),  # уже допустимая цена не меняется
        ("99999.5", UP, "100000"),  # перенос разряда
        ("123456.7", DOWN, "123456"),  # целая цена с 6 цифрами допустима
        ("9876.54", DOWN, "9876.5"),
        ("9876.54", UP, "9876.6"),
        ("9876.55", DOWN, "9876.5"),  # без банковского округления
    ],
)
def test_btc_price_rounding(price: str, rounding: Rounding, expected: str) -> None:
    result = BTC.round_price(Decimal(price), rounding)
    assert result == Decimal(expected)
    assert BTC.is_valid_price(result)


def test_btc_size_rounding_is_always_down() -> None:
    assert BTC.round_size_down(Decimal("0.0012399")) == Decimal("0.00123")
    assert BTC.round_size_down(Decimal("0.000009")) == Decimal("0")
    assert not BTC.is_valid_size(Decimal("0"))
    assert BTC.is_valid_size(Decimal("0.00001"))
    assert not BTC.is_valid_size(Decimal("0.000011"))


def test_invalid_inputs_rejected() -> None:
    with pytest.raises(PrecisionError):
        BTC.round_price(Decimal("0"), UP)
    with pytest.raises(PrecisionError):
        BTC.round_price(Decimal("-1"), DOWN)
    with pytest.raises(PrecisionError):
        BTC.round_size_down(Decimal("-0.1"))
    with pytest.raises(PrecisionError, match="слишком мала"):
        BTC.round_price(Decimal("0.01"), DOWN)  # у BTC шаг цены не мельче 0.1


def test_min_notional() -> None:
    assert BTC.meets_min_notional(Decimal("0.00016"), Decimal("65000"), Decimal("10"))
    assert not BTC.meets_min_notional(Decimal("0.00015"), Decimal("65000"), Decimal("10"))
    assert BTC.notional(Decimal("-0.1"), Decimal("65000")) == Decimal("6500")


@settings(max_examples=500, deadline=None)
@given(
    price=st.decimals(min_value=Decimal("1"), max_value=Decimal("200000"), places=1),
    sz=st.integers(min_value=0, max_value=5),
)
def test_min_size_for_notional_is_smallest_sufficient(price: Decimal, sz: int) -> None:
    r = rules(sz)
    min_notional = Decimal("10")
    size = r.min_size_for_notional(price, min_notional)
    assert r.meets_min_notional(size, price, min_notional)
    one_step_less = size - Decimal(1).scaleb(-sz)
    assert one_step_less <= 0 or not r.meets_min_notional(one_step_less, price, min_notional)


def test_min_size_for_notional_btc_example() -> None:
    # 0.00015 BTC × 64603 = 9.69 USDC — меньше минимума, нужно 0.00016.
    assert BTC.min_size_for_notional(Decimal("64603"), Decimal("10")) == Decimal("0.00016")


@pytest.mark.parametrize(
    ("value", "wire"),
    [("100", "100"), ("100.000", "100"), ("0.00100", "0.001"), ("65000.5", "65000.5"), ("0", "0")],
)
def test_to_wire(value: str, wire: str) -> None:
    assert to_wire(Decimal(value)) == wire


# --- свойства на случайных значениях ---

prices = st.decimals(
    min_value=Decimal("0.000001"),
    max_value=Decimal("10000000"),
    allow_nan=False,
    allow_infinity=False,
    places=9,
)
sz_decimals = st.integers(min_value=0, max_value=5)


@settings(max_examples=3000, deadline=None)
@given(price=prices, sz=sz_decimals, rounding=st.sampled_from(list(Rounding)))
def test_price_rounding_properties(price: Decimal, sz: int, rounding: Rounding) -> None:
    r = rules(sz)
    try:
        result = r.round_price(price, rounding)
    except PrecisionError as exc:
        # Единственная допустимая причина отказа: цена меньше минимального шага (округление вниз).
        assert "слишком мала" in str(exc)
        assert rounding is DOWN
        return

    assert r.is_valid_price(result)
    # Направление соблюдено.
    assert result <= price if rounding is DOWN else result >= price
    # Идемпотентность.
    assert r.round_price(result, rounding) == result
    # Погрешность не больше одного шага: 5 значащих цифр или минимальный десятичный шаг.
    step = max(price * Decimal("0.0001"), Decimal(1).scaleb(-r.max_price_decimals))
    assert abs(result - price) <= step
    if r.is_valid_price(price):
        assert result == price


@settings(max_examples=1000, deadline=None)
@given(
    size=st.decimals(min_value=0, max_value=Decimal("1000000"), places=10, allow_nan=False),
    sz=sz_decimals,
)
def test_size_rounding_properties(size: Decimal, sz: int) -> None:
    r = rules(sz)
    result = r.round_size_down(size)
    assert result <= size
    assert size - result < Decimal(1).scaleb(-sz)
    assert result == 0 or r.is_valid_size(result)
