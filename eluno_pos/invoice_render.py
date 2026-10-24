"""Tax invoice rendering: fixed-width text (thermal/A4 printers, matches sample_invoice.txt) and HTML."""
from __future__ import annotations

import html
from decimal import Decimal
from typing import List

from .domain.bill import Bill
from .domain.money import amount_in_words, format_inr, to_decimal_str
from .domain.parties import Store

WIDTH = 104
FOOTER = [
    "Prices are inclusive of GST. Intra-state supply: CGST + SGST. Taxable value = inclusive price less tax, per line.",
    "Lenses are made to order against the prescription on file and are not returnable once dispensed; frames may be returned within 7 days.",
]


def _rate(rate_bps: int) -> str:
    half = Decimal(rate_bps) / 200
    return ("%s" % half.normalize()).rstrip(".") + "%" if half % 1 else "%d%%" % half


def _row(no: str, desc: str, hsn: str, qty: str, mrp: str, disc: str, taxable: str, cgst: str, sgst: str, total: str) -> str:
    return "%-3s%-38.38s %-8s%5s%11s%10s%11s%13s%13s%11s" % (no, desc, hsn, qty, mrp, disc, taxable, cgst, sgst, total)


def _kv(left_label: str, left: str, right_label: str, right: str) -> str:
    return "%-11s: %-31s%-12s: %s" % (left_label, left, right_label, right)


def render_text(bill: Bill, store: Store) -> str:
    t = bill.totals
    out: List[str] = ["=" * WIDTH, "ELUNO".center(WIDTH), ("%s · %s" % (store.name, store.address)).center(WIDTH),
                      "TAX INVOICE".center(WIDTH), "=" * WIDTH]
    out.append(_kv("Invoice no", bill.invoice_no or "(not issued: %s)" % bill.status, "Date", (bill.paid_at or bill.business_date)[:10]))
    out.append(_kv("Store", bill.store_code, "Staff", bill.staff_id))
    out.append(_kv("Customer", bill.customer_name, "Phone", bill.customer_phone))
    out.append(_kv("Rx on file", ", ".join(bill.rx_ids) or "-", "Channel", bill.channel))
    if bill.parent_invoice_no:
        out.append(_kv("Remake of", bill.parent_invoice_no, "Reason", "%s (lab cost: %s)" % (bill.remake_reason, bill.remake_cost_bearer)))
    out.append("-" * WIDTH)
    out.append(_row("#", "Description", "HSN", "Qty", "MRP(incl)", "Discount", "Taxable", "CGST", "SGST", "Total"))
    out.append("-" * WIDTH)
    for i, l in enumerate(bill.lines, start=1):
        desc = l.description + (" (for %s)" % l.wearer_name if l.wearer_customer_id and l.wearer_customer_id != bill.customer_id else "")
        out.append(_row(str(i), desc, l.hsn, "1", format_inr(l.mrp), format_inr(l.discount), format_inr(l.taxable),
                        "%s %s" % (_rate(l.rate_bps), format_inr(l.cgst)), "%s %s" % (_rate(l.rate_bps), format_inr(l.sgst)), format_inr(l.gross)))
    out.append("-" * WIDTH)
    out.append(_row("", "TOTAL", "", "", format_inr(t.mrp), format_inr(t.discount), format_inr(t.taxable), format_inr(t.cgst), format_inr(t.sgst), format_inr(t.net)))
    out.append("=" * WIDTH)
    out.append("%-15s: %s" % ("Offers applied", ", ".join(o["offer_id"] for o in bill.offers) or "-"))
    if bill.manual_discount:
        out.append("%-15s: %s, approved by %s" % ("Manual discount", bill.manual_discount["reason_code"], bill.manual_discount["approver_id"]))
    method = (bill.payment_method or "-").upper()
    pay = "%s ₹%s" % (method, to_decimal_str(t.net)) if t.net else "No charge"
    ref = bill.charge_id if bill.payment_method in ("upi", "card") else None
    out.append("%-15s: %s%s" % ("Payment", pay, "  ref %s" % ref if ref else ""))
    out.append("%-15s: %s" % ("Amount in words", amount_in_words(t.net)))
    out.append("%-15s: %s" % ("Shopify ref", "%s (order %s)" % (bill.shopify_order_name, bill.shopify_order_id) if bill.shopify_order_id else "pending sync"))
    out.append("-" * WIDTH)
    out += FOOTER
    out.append("=" * WIDTH)
    return "\n".join(out) + "\n"


def render_credit_note_text(refund, bill: Bill, store: Store) -> str:
    """Credit note: reverses whole lines of the original invoice, tax exactly as charged."""
    out: List[str] = ["=" * WIDTH, "ELUNO".center(WIDTH), ("%s · %s" % (store.name, store.address)).center(WIDTH),
                      "CREDIT NOTE".center(WIDTH), "=" * WIDTH]
    out.append(_kv("Credit note", refund.credit_note_no or "(pending)", "Date", (refund.money_returned_at or refund.created_at)[:10]))
    out.append(_kv("Against", bill.invoice_no or "-", "Inv. date", (bill.paid_at or bill.business_date)[:10]))
    out.append(_kv("Customer", bill.customer_name, "Phone", bill.customer_phone))
    out.append(_kv("Reason", refund.reason_code, "Staff", refund.staff_id + (" / approved %s" % refund.approver_id if refund.approver_id else "")))
    out.append("-" * WIDTH)
    out.append(_row("#", "Description", "HSN", "Qty", "Refund", "", "Taxable", "CGST", "SGST", "Total"))
    out.append("-" * WIDTH)
    for i, rl in enumerate(refund.lines, start=1):
        l = bill.line(rl.line_ref)
        out.append(_row(str(i), l.description, l.hsn, "1", format_inr(rl.gross), "", format_inr(rl.taxable),
                        "%s %s" % (_rate(l.rate_bps), format_inr(rl.cgst)), "%s %s" % (_rate(l.rate_bps), format_inr(rl.sgst)), format_inr(rl.gross)))
    out.append("-" * WIDTH)
    out.append(_row("", "TOTAL", "", "", format_inr(refund.amount), "", format_inr(refund.taxable),
                    format_inr(sum(l.cgst for l in refund.lines)), format_inr(sum(l.sgst for l in refund.lines)), format_inr(refund.amount)))
    out.append("=" * WIDTH)
    out.append("%-15s: %s ₹%s%s" % ("Refunded via", refund.method.upper(), to_decimal_str(refund.amount),
                                    "  ref %s" % refund.terminal_refund_id if refund.terminal_refund_id else ""))
    out.append("%-15s: %s" % ("Amount in words", amount_in_words(refund.amount)))
    out.append("%-15s: %s" % ("Restocked", ", ".join("%s at %s" % (l.sku, bill.store_code) for l in refund.lines if l.restock)
                              or "none (lenses are made to order)"))
    out.append("%-15s: %s" % ("Shopify ref", "%s refund %s" % (bill.shopify_order_name, refund.shopify_refund_id) if refund.shopify_refund_id else "pending sync"))
    out.append("=" * WIDTH)
    return "\n".join(out) + "\n"


def render_html(bill: Bill, store: Store) -> str:
    e = html.escape
    t = bill.totals
    rows = "".join(
        "<tr><td>%d</td><td>%s</td><td>%s</td><td class=n>1</td><td class=n>%s</td><td class=n>%s</td><td class=n>%s</td>"
        "<td class=n>%s %s</td><td class=n>%s %s</td><td class=n>%s</td></tr>" % (
            i, e(l.description), e(l.hsn), format_inr(l.mrp), format_inr(l.discount), format_inr(l.taxable),
            _rate(l.rate_bps), format_inr(l.cgst), _rate(l.rate_bps), format_inr(l.sgst), format_inr(l.gross))
        for i, l in enumerate(bill.lines, start=1))
    remake = ("<p><b>Remake of</b> %s &middot; %s &middot; lab cost: %s</p>" % (e(bill.parent_invoice_no), e(bill.remake_reason or ""), e(bill.remake_cost_bearer or ""))
              if bill.parent_invoice_no else "")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Invoice %(inv)s</title>
<style>body{font:13px/1.4 system-ui,sans-serif;margin:24px;color:#111}h1{margin:0;font-size:20px;letter-spacing:.2em}
.meta{display:grid;grid-template-columns:repeat(2,1fr);gap:2px 24px;margin:12px 0}table{border-collapse:collapse;width:100%%}
th,td{border-bottom:1px solid #ccc;padding:4px 6px;text-align:left}td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}
tfoot td{font-weight:600;border-top:2px solid #111}.foot{color:#555;font-size:11px;margin-top:16px}@media print{body{margin:0}}</style></head>
<body><h1>ELUNO</h1><div>%(store)s &middot; %(addr)s</div><h2>TAX INVOICE</h2>
<div class="meta"><div><b>Invoice no</b> %(inv)s</div><div><b>Date</b> %(date)s</div><div><b>Store</b> %(code)s</div><div><b>Staff</b> %(staff)s</div>
<div><b>Customer</b> %(cust)s</div><div><b>Phone</b> %(phone)s</div><div><b>Rx on file</b> %(rx)s</div><div><b>Channel</b> %(chan)s</div></div>%(remake)s
<table><thead><tr><th>#</th><th>Description</th><th>HSN</th><th class=n>Qty</th><th class=n>MRP (incl)</th><th class=n>Discount</th><th class=n>Taxable</th>
<th class=n>CGST</th><th class=n>SGST</th><th class=n>Total</th></tr></thead><tbody>%(rows)s</tbody>
<tfoot><tr><td></td><td>TOTAL</td><td></td><td></td><td class=n>%(mrp)s</td><td class=n>%(disc)s</td><td class=n>%(taxable)s</td><td class=n>%(cgst)s</td>
<td class=n>%(sgst)s</td><td class=n>%(net)s</td></tr></tfoot></table>
<p><b>Offers applied</b> %(offers)s<br><b>Payment</b> %(pay)s<br><b>Amount in words</b> %(words)s<br><b>Shopify ref</b> %(shop)s</p>
<div class="foot">%(foot)s</div></body></html>""" % {
        "inv": e(bill.invoice_no or ""), "store": e(store.name), "addr": e(store.address), "date": e((bill.paid_at or bill.business_date)[:10]),
        "code": e(bill.store_code), "staff": e(bill.staff_id), "cust": e(bill.customer_name), "phone": e(bill.customer_phone),
        "rx": e(", ".join(bill.rx_ids) or "-"), "chan": e(bill.channel), "remake": remake, "rows": rows,
        "mrp": format_inr(t.mrp), "disc": format_inr(t.discount), "taxable": format_inr(t.taxable), "cgst": format_inr(t.cgst),
        "sgst": format_inr(t.sgst), "net": format_inr(t.net), "offers": e(", ".join(o["offer_id"] for o in bill.offers) or "-"),
        "pay": e("%s ₹%s %s" % ((bill.payment_method or "").upper(), to_decimal_str(t.net), bill.charge_id or "")),
        "words": e(amount_in_words(t.net)), "shop": e("%s (order %s)" % (bill.shopify_order_name, bill.shopify_order_id) if bill.shopify_order_id else "pending sync"),
        "foot": "<br>".join(e(f) for f in FOOTER)}
