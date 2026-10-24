"""Cart lines + offer evaluation + tax table -> priced lines and totals, exact to the paisa."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Sequence, Tuple

from .offers import OfferEvaluation
from .tax import LineTax, TaxTable, back_out_gst


@dataclass(frozen=True)
class PricedLine:
    line_ref: str
    mrp: int
    discounts: Tuple[Tuple[str, int], ...]  # (offer_id, amount)
    tax: LineTax

    @property
    def discount(self) -> int:
        return sum(a for _, a in self.discounts)

    @property
    def gross(self) -> int:
        return self.tax.gross


@dataclass(frozen=True)
class Totals:
    mrp: int
    discount: int
    net: int
    taxable: int
    cgst: int
    sgst: int

    @property
    def tax(self) -> int:
        return self.cgst + self.sgst

    def to_dict(self) -> dict:
        return {"mrp": self.mrp, "discount": self.discount, "net": self.net, "taxable": self.taxable,
                "cgst": self.cgst, "sgst": self.sgst, "tax": self.tax}


def price_line(line_ref: str, mrp: int, hsn: str, discounts: Sequence[Tuple[str, int]], taxes: TaxTable, day: date) -> PricedLine:
    disc = sum(a for _, a in discounts)
    if disc > mrp:
        raise ValueError("discount %d exceeds MRP %d on %s" % (disc, mrp, line_ref))
    rate = taxes.rate_for(hsn, day)
    return PricedLine(line_ref, mrp, tuple(discounts), back_out_gst(mrp - disc, rate.rate_bps))


def price(lines: Sequence[Tuple[str, int, str]], evaluation: OfferEvaluation, taxes: TaxTable, day: date) -> List[PricedLine]:
    """`lines` are (line_ref, mrp, hsn). Discounts come from the evaluation, GST from the table on `day`."""
    return [price_line(ref, mrp, hsn, evaluation.for_line(ref), taxes, day) for ref, mrp, hsn in lines]


def totals(lines: Sequence[PricedLine]) -> Totals:
    t = Totals(mrp=sum(l.mrp for l in lines), discount=sum(l.discount for l in lines), net=sum(l.gross for l in lines),
               taxable=sum(l.tax.taxable for l in lines), cgst=sum(l.tax.cgst for l in lines), sgst=sum(l.tax.sgst for l in lines))
    if not (t.taxable + t.cgst + t.sgst == t.net == t.mrp - t.discount):
        raise ArithmeticError("bill totals do not reconcile: %r" % (t,))
    return t
