"""GST per line by HSN. Rates are data with effective dates; nothing here hardcodes a rate.

Prices are MRP inclusive of GST. Per line:
    taxable = gross * 10000 / (10000 + rate_bps), rounded half-up to the paisa
    tax     = gross - taxable
    CGST    = tax / 2 rounded half-up; SGST = the remainder
so taxable + CGST + SGST == gross exactly (the reference is validate_seed.py).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Dict, Iterable, List, Optional

from .errors import ConfigError, DomainError
from .money import div_round_half_up


@dataclass(frozen=True)
class TaxRate:
    hsn: str
    rate_bps: int
    effective_from: date
    effective_to: Optional[date]
    description: str = ""

    def covers(self, day: date) -> bool:
        return self.effective_from <= day and (self.effective_to is None or day <= self.effective_to)

    @property
    def half_rate(self) -> float:
        """CGST / SGST rate as Shopify's tax_line.rate wants it (0.09 for 18%)."""
        return self.rate_bps / 20000


@dataclass(frozen=True)
class LineTax:
    gross: int
    taxable: int
    cgst: int
    sgst: int
    rate_bps: int

    @property
    def tax(self) -> int:
        return self.cgst + self.sgst


def back_out_gst(gross: int, rate_bps: int) -> LineTax:
    if gross < 0:
        raise DomainError("a line cannot have a negative gross amount", code="negative_gross")
    taxable = div_round_half_up(gross * 10000, 10000 + rate_bps)
    tax = gross - taxable
    cgst = div_round_half_up(tax, 2)
    return LineTax(gross=gross, taxable=taxable, cgst=cgst, sgst=tax - cgst, rate_bps=rate_bps)


class TaxTable:
    def __init__(self, rates: Iterable[TaxRate]):
        self._by_hsn: Dict[str, List[TaxRate]] = {}
        for r in rates:
            self._by_hsn.setdefault(r.hsn, []).append(r)
        for hsn, rs in self._by_hsn.items():
            rs.sort(key=lambda r: r.effective_from)
            for a, b in zip(rs, rs[1:]):
                if a.effective_to is None or a.effective_to >= b.effective_from:
                    raise ConfigError("tax rates for HSN %s overlap (%s and %s)" % (hsn, a.effective_from, b.effective_from))

    @classmethod
    def from_json(cls, data: dict) -> "TaxTable":
        try:
            return cls(TaxRate(hsn=str(r["hsn"]), rate_bps=int(r["rate_bps"]),
                               effective_from=date.fromisoformat(r["effective_from"]),
                               effective_to=date.fromisoformat(r["effective_to"]) if r.get("effective_to") else None,
                               description=r.get("description", ""))
                       for r in data["rates"])
        except (KeyError, ValueError, TypeError) as e:
            raise ConfigError("tax_rates.json is malformed: %s" % e)

    def rate_for(self, hsn: str, day: date) -> TaxRate:
        for r in self._by_hsn.get(hsn, []):
            if r.covers(day):
                return r
        raise DomainError("No GST rate for HSN %s on %s. Ask finance to add it to tax_rates.json; billing this item is blocked until then." % (hsn, day),
                          code="tax_rate_missing")

    def hsns(self) -> List[str]:
        return sorted(self._by_hsn)
