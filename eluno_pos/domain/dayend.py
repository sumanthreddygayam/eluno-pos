"""Day-end report for one store and one date.

Source of truth is Shopify (finance reconciles there). The POS's own ledger (bills, refunds, till)
is used only to *check* Shopify: anything paid at the till but missing or different in Shopify is
listed as a reconciliation finding instead of being silently added to the numbers.

Sales count on the day the invoice was issued; refunds count on the day they were made
(a refund today of last week's bill reduces today's takings, which is what the drawer sees).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

IST = timezone(timedelta(hours=5, minutes=30), "IST")


@dataclass(frozen=True)
class RefundRecord:
    refund_id: int
    created_at: datetime
    amount: int
    taxable: int
    tax: int
    credit_note_no: Optional[str] = None
    reason_code: Optional[str] = None


@dataclass(frozen=True)
class SaleRecord:
    """A POS order as read back from the system of record."""
    order_id: int
    order_name: str
    invoice_no: str
    store_code: str
    location_id: int
    created_at: datetime
    channel: str
    payment_method: str
    staff_id: Optional[str]
    mrp: int
    discount: int
    net: int
    taxable: int
    cgst: int
    sgst: int
    captured: int
    discounts_by_offer: Tuple[Tuple[str, int], ...] = ()
    parent_invoice_no: Optional[str] = None
    refunds: Tuple[RefundRecord, ...] = ()

    @property
    def tax(self) -> int:
        return self.cgst + self.sgst


@dataclass
class MethodLine:
    bills: int = 0
    sales: int = 0
    refunds: int = 0

    @property
    def net(self) -> int:
        return self.sales - self.refunds


@dataclass
class Finding:
    severity: str  # error | warning | info
    message: str

    def to_dict(self) -> dict:
        return {"severity": self.severity, "message": self.message}


@dataclass
class DayEndReport:
    store_code: str
    day: date
    bills: int = 0
    zero_value_bills: int = 0
    gross_mrp: int = 0
    discounts: int = 0
    net_sales: int = 0
    taxable: int = 0
    cgst: int = 0
    sgst: int = 0
    refunds_count: int = 0
    refunds_amount: int = 0
    refunds_taxable: int = 0
    refunds_tax: int = 0
    by_payment_method: Dict[str, MethodLine] = field(default_factory=dict)
    by_channel: Dict[str, MethodLine] = field(default_factory=dict)
    discounts_by_offer: Dict[str, int] = field(default_factory=dict)
    invoices: List[str] = field(default_factory=list)
    credit_notes: List[str] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    till: Optional[dict] = None

    @property
    def tax(self) -> int:
        return self.cgst + self.sgst

    @property
    def net_after_refunds(self) -> int:
        return self.net_sales - self.refunds_amount

    @property
    def average_bill(self) -> int:
        paid = self.bills - self.zero_value_bills
        return self.net_sales // paid if paid else 0

    def to_dict(self) -> dict:
        def ml(d: Dict[str, MethodLine]) -> dict:
            return {k: {"bills": v.bills, "sales": v.sales, "refunds": v.refunds, "net": v.net} for k, v in sorted(d.items())}
        return {
            "store_code": self.store_code, "date": self.day.isoformat(), "currency": "INR", "unit": "paise",
            "bills": self.bills, "zero_value_bills": self.zero_value_bills, "gross_mrp": self.gross_mrp,
            "discounts": self.discounts, "net_sales": self.net_sales, "taxable": self.taxable,
            "cgst": self.cgst, "sgst": self.sgst, "tax": self.tax, "average_bill": self.average_bill,
            "refunds": {"count": self.refunds_count, "amount": self.refunds_amount, "taxable": self.refunds_taxable, "tax": self.refunds_tax},
            "net_after_refunds": self.net_after_refunds,
            "by_payment_method": ml(self.by_payment_method), "by_channel": ml(self.by_channel),
            "discounts_by_offer": dict(sorted(self.discounts_by_offer.items())),
            "invoices": self.invoices, "credit_notes": self.credit_notes,
            "findings": [f.to_dict() for f in self.findings], "till": self.till,
        }


def local_day(ts: datetime) -> date:
    return ts.astimezone(IST).date()


def build_day_end(store_code: str, day: date, records: Iterable[SaleRecord]) -> DayEndReport:
    rep = DayEndReport(store_code=store_code, day=day)
    for r in records:
        if r.store_code != store_code:
            continue
        if local_day(r.created_at) == day:
            rep.bills += 1
            rep.zero_value_bills += 1 if r.net == 0 else 0
            rep.gross_mrp += r.mrp
            rep.discounts += r.discount
            rep.net_sales += r.net
            rep.taxable += r.taxable
            rep.cgst += r.cgst
            rep.sgst += r.sgst
            m = rep.by_payment_method.setdefault(r.payment_method, MethodLine())
            m.bills += 1
            m.sales += r.net
            c = rep.by_channel.setdefault(r.channel, MethodLine())
            c.bills += 1
            c.sales += r.net
            for title, amt in r.discounts_by_offer:
                rep.discounts_by_offer[title] = rep.discounts_by_offer.get(title, 0) + amt
            rep.invoices.append(r.invoice_no)
            if r.captured != r.net:
                rep.findings.append(Finding("error", "%s (%s): captured %d paise but invoice total is %d." % (r.invoice_no, r.order_name, r.captured, r.net)))
        for rf in r.refunds:
            if local_day(rf.created_at) != day:
                continue
            rep.refunds_count += 1
            rep.refunds_amount += rf.amount
            rep.refunds_taxable += rf.taxable
            rep.refunds_tax += rf.tax
            rep.by_payment_method.setdefault(r.payment_method, MethodLine()).refunds += rf.amount
            rep.by_channel.setdefault(r.channel, MethodLine()).refunds += rf.amount
            if rf.credit_note_no:
                rep.credit_notes.append(rf.credit_note_no)
            else:
                rep.findings.append(Finding("error", "Refund %d on %s has no credit note number." % (rf.refund_id, r.invoice_no)))
    rep.invoices.sort()
    rep.credit_notes.sort()
    return rep


def reconcile(rep: DayEndReport, shopify: Sequence[SaleRecord], local_bills: Sequence, local_refunds: Sequence) -> None:
    """Compare the POS ledger (bills/refunds made by this till on that day) with Shopify; add findings."""
    by_invoice = {r.invoice_no: r for r in shopify}
    for b in local_bills:
        if b.status == "paid":
            rep.findings.append(Finding("error", "%s: payment captured (%s, Rs %s) but the order is not in Shopify yet. Run sync; last error: %s" % (
                b.invoice_no, b.payment_method, _rs(b.total), b.last_error or "none")))
        elif b.status == "voided":
            rep.findings.append(Finding("info", "%s: voided before reaching Shopify (%s); stays in the series as cancelled." % (b.invoice_no, b.void_reason)))
        elif b.status == "synced":
            s = by_invoice.get(b.invoice_no)
            if s is None:
                rep.findings.append(Finding("error", "%s: marked synced locally but not found in Shopify (order %s)." % (b.invoice_no, b.shopify_order_name)))
            elif s.net != b.total or s.tax != b.totals.tax:
                rep.findings.append(Finding("error", "%s: Shopify total %s / tax %s differ from the invoice %s / %s." % (
                    b.invoice_no, _rs(s.net), _rs(s.tax), _rs(b.total), _rs(b.totals.tax))))
    for rf in local_refunds:
        if rf.status == "money_returned":
            rep.findings.append(Finding("error", "Credit note %s: money returned (Rs %s) but the refund is not in Shopify yet. Run sync." % (rf.credit_note_no, _rs(rf.amount))))
    local_invoices = {b.invoice_no for b in local_bills if b.invoice_no}
    foreign = [i for i in rep.invoices if i not in local_invoices]
    if foreign:
        rep.findings.append(Finding("info", "%d invoice(s) in Shopify were not issued by this POS database (e.g. pre-cutover or another till): %s" % (len(foreign), ", ".join(foreign))))


def _rs(paise: int) -> str:
    return "%d.%02d" % (paise // 100, paise % 100)
