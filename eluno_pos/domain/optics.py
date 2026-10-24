"""Dioptre arithmetic. Powers are held as integer hundredths of a dioptre (-6.00 D == -600)
so range checks never compare floats."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Optional, Union

Number = Union[int, float, str, Decimal]

QUARTER = 25  # the 0.25 D grid


def to_cd(value: Optional[Number]) -> Optional[int]:
    """1.5 -> 150; None stays None. Raises if the value is not on a 0.01 D step."""
    if value is None:
        return None
    try:
        d = Decimal(str(value)) * 100
    except InvalidOperation:
        raise ValueError("not a dioptre value: %r" % (value,))
    if d != d.to_integral_value():
        raise ValueError("dioptre value %r has more than two decimals" % (value,))
    return int(d)


def cd_to_float(cd: Optional[int]) -> Optional[float]:
    return None if cd is None else cd / 100


def fmt_d(cd: Optional[int]) -> str:
    """-600 -> '-6.00', 250 -> '+2.50', 0 -> '0.00'."""
    if cd is None:
        return "none"
    sign = "+" if cd > 0 else ("-" if cd < 0 else "")
    a = abs(cd)
    return "%s%d.%02d" % (sign, a // 100, a % 100)


def normalize_index(value: Number) -> str:
    """1.6, '1.6', '1.60' -> '1.60' (the lens matrix key)."""
    try:
        return str(Decimal(str(value)).quantize(Decimal("0.01")))
    except InvalidOperation:
        raise ValueError("not a lens index: %r" % (value,))


def short_index(index: str) -> str:
    """'1.60' -> '1.6', '1.56' -> '1.56'. Matches the lens_config property already in Shopify."""
    s = str(Decimal(index).normalize())
    return s if "." in s else s + ".0"
