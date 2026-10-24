"""Money is integer paise everywhere inside the POS.

Decimal strings ("104.98") exist only at the Shopify boundary; display formatting
(Indian digit grouping, amount in words) exists only at the invoice boundary.
"""
from __future__ import annotations

import re
from typing import List, Sequence

_DECIMAL_STR = re.compile(r"^(-?)(\d+)(?:\.(\d{1,2}))?$")


def div_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounded half away from zero (half-up for positive amounts)."""
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    q, r = divmod(abs(numerator), denominator)
    if 2 * r >= denominator:
        q += 1
    return q if numerator >= 0 else -q


def percent_of(amount: int, bps: int) -> int:
    """`bps` basis points of `amount`, rounded half-up to the paisa."""
    return div_round_half_up(amount * bps, 10000)


def to_decimal_str(paise: int) -> str:
    """10498 -> "104.98". The only way money leaves the core for Shopify."""
    if not isinstance(paise, int) or isinstance(paise, bool):
        raise TypeError("money must be integer paise, got %r" % (paise,))
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    return "%s%d.%02d" % (sign, p // 100, p % 100)


def from_decimal_str(value: str) -> int:
    """"104.98" -> 10498. Rejects floats and anything with more than two decimals."""
    if not isinstance(value, str):
        raise TypeError("expected a decimal string, got %r" % (value,))
    m = _DECIMAL_STR.match(value.strip())
    if not m:
        raise ValueError("not a money string: %r" % (value,))
    sign, whole, frac = m.groups()
    paise = int(whole) * 100 + int((frac or "0").ljust(2, "0"))
    return -paise if sign else paise


def rupees(value: int) -> int:
    """Readable constant helper: rupees(500) == 50000 paise."""
    return value * 100


def format_inr(paise: int) -> str:
    """Indian digit grouping: 1234567.8 -> "12,34,567.80"."""
    sign = "-" if paise < 0 else ""
    p = abs(paise)
    whole, frac = str(p // 100), p % 100
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return "%s%s.%02d" % (sign, whole, frac)


def allocate(total: int, weights: Sequence[int]) -> List[int]:
    """Split `total` paise across `weights` pro rata; the parts always sum to `total`.

    Largest-remainder method, ties to the earliest index, so the result is deterministic.
    """
    if total < 0:
        raise ValueError("cannot allocate a negative amount")
    if not weights:
        if total:
            raise ValueError("nothing to allocate %d paise to" % total)
        return []
    if any(w < 0 for w in weights):
        raise ValueError("weights must be non-negative")
    wsum = sum(weights)
    if wsum == 0:
        if total:
            raise ValueError("cannot allocate across zero weights")
        return [0] * len(weights)
    parts = [total * w // wsum for w in weights]
    remainders = [(total * w % wsum, -i) for i, w in enumerate(weights)]
    short = total - sum(parts)
    for _, neg_i in sorted(remainders, reverse=True)[:short]:
        parts[-neg_i] += 1
    return parts


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]


def _below_100(n: int) -> str:
    if n < 20:
        return _ONES[n]
    t, o = divmod(n, 10)
    return _TENS[t] + ("-" + _ONES[o] if o else "")


def _below_1000(n: int) -> str:
    h, rest = divmod(n, 100)
    parts = []
    if h:
        parts.append(_ONES[h] + " hundred")
    if rest:
        parts.append(_below_100(rest))
    return " ".join(parts)


def _indian_words(n: int) -> str:
    if n == 0:
        return "zero"
    parts = []
    crore, n = divmod(n, 10_000_000)
    lakh, n = divmod(n, 100_000)
    thousand, n = divmod(n, 1000)
    if crore:
        parts.append(_indian_words(crore) + " crore")
    if lakh:
        parts.append(_below_100(lakh) + " lakh")
    if thousand:
        parts.append(_below_100(thousand) + " thousand")
    if n:
        parts.append(_below_1000(n))
    return " ".join(parts)


def amount_in_words(paise: int) -> str:
    """1907215 -> "Nineteen thousand seventy-two rupees and fifteen paise"."""
    if paise < 0:
        raise ValueError("amount in words is only defined for non-negative amounts")
    r, p = divmod(paise, 100)
    text = _indian_words(r) + (" rupee" if r == 1 else " rupees")
    text += (" and " + _below_100(p) + " paise") if p else " only"
    return text[0].upper() + text[1:]
