"""Command-line till. Every command maps to one application use case.

    python -m eluno_pos lookup --phone +919000000005
    python -m eluno_pos bill --store HSR --staff S105 --phone +919000000005 \
        --pair frame=FR-0003,lens=progressive/1.60/blue_cut --pay upi
    python -m eluno_pos refund ELU/HSR/FY26/00003 --store HSR --staff S105 --reason CHANGED_MIND
    python -m eluno_pos day-end --store HSR --date 2026-10-22
Run `python -m eluno_pos <command> -h` for options. Amounts on the command line are in rupees.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from typing import Any, List, Optional

from .app import PosApp
from .app.billing import BillRequest, ManualDiscountRequest, PairRequest, Quote
from .config import BusinessConfig, Settings
from .domain.bill import Bill
from .domain.dayend import DayEndReport
from .domain.errors import BillBlocked, DomainError, InvalidRequest
from .domain.money import format_inr, from_decimal_str
from .domain.refund import Refund
from .domain.till import TillSession
from .invoice_render import render_credit_note_text, render_html, render_text
from .ports import IntegrationError


def rs(paise: Optional[int]) -> str:
    return "Rs %s" % format_inr(paise or 0)


def rupees_arg(text: str) -> int:
    try:
        return from_decimal_str(text)
    except ValueError:
        raise argparse.ArgumentTypeError("amount must be rupees, like 3000 or 120.50")


# ------------------------------------------------------------------ printing
def print_issues(issues: List[Any]) -> None:
    for i in issues:
        print("  " + str(i))


def print_quote(q: Quote) -> None:
    c = q.customer
    print("Customer : %s  %s  %s" % (c.name, c.phone, " ".join("[%s]" % f for f in c.billing_flags())))
    print("Store    : %s   Channel: %s   Offer policy: %s" % (q.store.store_code, q.request.channel, q.evaluation.policy))
    by_ref = {p.line_ref: p for p in q.priced}
    print("%-4s %-46s %12s %10s %12s %10s %12s" % ("Line", "Item", "MRP", "Discount", "Taxable", "GST", "Total"))
    for l in q.lines:
        p = by_ref[l.line_ref]
        print("%-4s %-46.46s %12s %10s %12s %10s %12s" % (l.line_ref, l.description + ("" if l.wearer.customer_id == c.customer_id else " (for %s)" % l.wearer.name),
                                                        format_inr(p.mrp), format_inr(p.discount), format_inr(p.tax.taxable), format_inr(p.tax.tax), format_inr(p.gross)))
    t = q.totals
    print("%-51s %12s %10s %12s %10s %12s" % ("TOTAL", format_inr(t.mrp), format_inr(t.discount), format_inr(t.taxable), format_inr(t.tax), format_inr(t.net)))
    print("Offers:")
    for d in sorted(q.evaluation.decisions, key=lambda d: not d.applied):
        print("  %s %-17s %10s  %s" % ("+" if d.applied else "-", d.offer_id, format_inr(d.amount) if d.applied else "", d.why))
    if q.issues:
        print("Checks:")
        print_issues(q.issues)


def print_bill(b: Bill) -> None:
    print("Bill %s  status=%s  invoice=%s  total=%s  method=%s  shopify=%s" % (
        b.bill_id, b.status.upper(), b.invoice_no or "-", rs(b.total), b.payment_method or "-", b.shopify_order_name or "-"))
    if b.status == "paid" and b.last_error:
        print("  Payment taken and invoice issued; Shopify sync pending (%s). It will be retried by `sync`." % b.last_error)


def print_refund(r: Refund) -> None:
    print("Refund %s  status=%s  credit note=%s  amount=%s (tax reversed %s)  method=%s" % (
        r.refund_id, r.status.upper(), r.credit_note_no or "-", rs(r.amount), rs(r.tax), r.method))
    for l in r.lines:
        print("  %-3s %-9s %-8s gross %10s  taxable %10s  tax %8s  %s" % (l.line_ref, l.sku, l.kind, format_inr(l.gross), format_inr(l.taxable),
                                                                       format_inr(l.tax), "restock at %s" % l.location_id if l.restock else "no restock"))
    if r.last_error:
        print("  last error: %s" % r.last_error)


def print_till(s: TillSession) -> None:
    print("Till %s  %s  float %s  expected %s%s" % (s.session_id, s.status.upper(), rs(s.opening_float_paise), rs(s.expected_cash_paise),
                                                  "  counted %s  variance %s%s" % (rs(s.counted_cash_paise), rs(s.variance_paise),
                                                                                    "  ** FLAGGED to finance **" if s.variance_flagged else "")
                                                  if s.counted_cash_paise is not None else ""))
    for m in s.movements:
        print("  %s %-11s %12s  %-18s %s" % (m.at[11:16], m.type, format_inr(m.amount_paise), m.ref, m.reason or ""))


def print_report(r: DayEndReport) -> None:
    print("DAY-END  %s  %s   (source: Shopify orders; checked against the POS ledger and till)" % (r.store_code, r.day))
    rows = [("Bills", str(r.bills) + (" (%d zero-value)" % r.zero_value_bills if r.zero_value_bills else "")), ("Gross (MRP incl GST)", rs(r.gross_mrp)),
            ("Discounts", rs(r.discounts)), ("Net sales", rs(r.net_sales)), ("Taxable value", rs(r.taxable)), ("CGST", rs(r.cgst)), ("SGST", rs(r.sgst)),
            ("Refunds", "%s (%d, tax reversed %s)" % (rs(r.refunds_amount), r.refunds_count, rs(r.refunds_tax))),
            ("Net after refunds", rs(r.net_after_refunds)), ("Average bill", rs(r.average_bill))]
    for k, v in rows:
        print("  %-22s %s" % (k, v))
    print("  By payment method:")
    for k, m in sorted(r.by_payment_method.items()):
        print("    %-8s bills %2d  sales %14s  refunds %12s  net %14s" % (k, m.bills, format_inr(m.sales), format_inr(m.refunds), format_inr(m.net)))
    print("  By channel:")
    for k, m in sorted(r.by_channel.items()):
        print("    %-16s bills %2d  net %14s" % (k, m.bills, format_inr(m.net)))
    if r.discounts_by_offer:
        print("  Discounts by offer: " + ", ".join("%s %s" % (k, format_inr(v)) for k, v in sorted(r.discounts_by_offer.items())))
    if r.till:
        t = r.till
        print("  Till: float %s + cash sales %s - cash refunds %s + paid-in %s - paid-out %s = expected %s; counted %s; variance %s%s" % (
            format_inr(t["opening_float"]), format_inr(t["cash_sales"]), format_inr(t["cash_refunds"]), format_inr(t["paid_in"]),
            format_inr(t["paid_out"]), format_inr(t["expected"]), format_inr(t["counted"]) if t["counted"] is not None else "-",
            format_inr(t["variance"]) if t["variance"] is not None else "-", "  FLAGGED" if t["flagged"] else ""))
    print("  Invoices: %s" % (", ".join(r.invoices) or "-"))
    if r.credit_notes:
        print("  Credit notes: %s" % ", ".join(r.credit_notes))
    print("  Reconciliation: %s" % ("clean" if not r.findings else ""))
    for f in r.findings:
        print("    [%s] %s" % (f.severity.upper(), f.message))


def out_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


# ------------------------------------------------------------------ commands
def bill_request(a: argparse.Namespace) -> BillRequest:
    md = None
    if a.discount:
        kv = dict(p.split("=", 1) for p in a.discount.split(",") if "=" in p)
        try:
            md = ManualDiscountRequest(bps=int(round(float(kv["pct"]) * 100)) if "pct" in kv else int(kv["bps"]), approver_id=kv["approver"],
                                       reason_code=kv.get("reason", ""))
        except (KeyError, ValueError):
            raise InvalidRequest("--discount must look like pct=10,approver=S108,reason=WALKOUT_SAVE")
    return BillRequest(store_code=a.store, staff_id=a.staff, customer_phone=a.phone, channel=a.channel,
                       pairs=tuple(PairRequest.parse(p) for p in a.pair), manual_discount=md, acknowledge=tuple(a.ack or ()))


def cmd_lookup(app: PosApp, a) -> None:
    v = app.billing.lookup(a.phone)
    if a.json:
        return out_json(v)
    c = v["customer"]
    print("%s  %s  %s  lifetime %s" % (c["name"], c["phone"], " ".join("[%s]" % f for f in c["flags"]) or "", rs(c["lifetime_value"])))
    rx = v["prescription"]
    if rx:
        print("Rx %s (%s, %s to %s)  OD %s  OS %s  PD %s%s" % (rx["rx_id"], rx["source"], rx["issued_on"], rx["valid_until"],
                                                            _eye(rx["od"]), _eye(rx["os"]), rx["pd"], ("  note: " + rx["notes"]) if rx["notes"] else ""))
    else:
        print("No valid prescription.")
    for s in v["skipped_prescriptions"]:
        print("  skipped %s: %s" % (s["rx_id"], s["reason"]))
    for f in v["flags"]:
        print("  [%s] %s %s" % (f["severity"].upper(), f["message"], f.get("hint", "")))


def _eye(e: dict) -> str:
    def d(cd):
        return "%+.2f" % (cd / 100) if cd else ("0.00" if cd == 0 else "-")
    return "sph %s cyl %s axis %s add %s" % (d(e["sph"]), d(e["cyl"]), e["axis"] if e["axis"] is not None else "-", d(e["add"]) if e["add"] is not None else "-")


def cmd_quote(app: PosApp, a) -> None:
    q = app.billing.quote(bill_request(a))
    if a.json:
        return out_json(q.to_dict())
    print_quote(q)
    print("=> %s" % ("BLOCKED: fix the issues above" if q.blocking else "OK to bill"))


def cmd_bill(app: PosApp, a) -> None:
    bill, q = app.billing.create(bill_request(a))
    if not a.json:
        print_quote(q)
        print_bill(bill)
    if a.pay:
        bill = app.billing.pay(bill.bill_id, a.pay, a.staff)
        if not a.json:
            print_bill(bill)
    if a.json:
        out_json(bill.to_dict())


def cmd_pay(app: PosApp, a) -> None:
    b = app.billing.pay(a.bill_id, a.method, a.staff)
    out_json(b.to_dict()) if a.json else print_bill(b)


def cmd_cancel(app: PosApp, a) -> None:
    print_bill(app.billing.cancel(a.bill_id, a.staff, a.why, a.approver))


def cmd_void(app: PosApp, a) -> None:
    print_bill(app.billing.void(a.bill_id, a.staff, a.approver, a.reason))


def cmd_cutover(app: PosApp, a) -> None:
    for series, info in app.billing.cutover(a.store or ()).items():
        print("%-22s continues after %05d (last issued %05d)" % (series, info["floor"], info["last"]))


def cmd_sync(app: PosApp, a) -> None:
    lines = app.sync_pending()
    print("\n".join(lines) if lines else "Nothing pending.")


def cmd_refund(app: PosApp, a) -> None:
    r = app.refunds.refund(a.invoice, a.store, a.staff, a.reason, a.lines.split(",") if a.lines else None, a.approver, a.note)
    out_json(r.to_dict()) if a.json else print_refund(r)


def cmd_remake(app: PosApp, a) -> None:
    b = app.remakes.remake(a.parent_invoice, a.store, a.staff, a.reason, a.cost_bearer, a.pair, a.lens, tuple(a.ack or ()))
    out_json(b.to_dict()) if a.json else print_bill(b)


def cmd_till(app: PosApp, a) -> None:
    if a.action == "open":
        s = app.tills.open(a.store, a.staff, a.amount or 0)
    elif a.action in ("paid-in", "paid-out"):
        if not a.amount or not a.reason:
            raise InvalidRequest("%s needs --amount and --reason" % a.action)
        s = app.tills.move(a.store, a.staff, a.action.replace("-", "_"), a.amount, a.reason, a.note)
    elif a.action == "close":
        if a.amount is None:
            raise InvalidRequest("close needs --amount (the counted cash)")
        s = app.tills.close(a.store, a.staff, a.amount, a.note)
    else:
        s = app.tills.current(a.store)
    out_json(s.to_dict()) if a.json else print_till(s)


def cmd_day_end(app: PosApp, a) -> None:
    r = app.reports.day_end(a.store, date.fromisoformat(a.date) if a.date else app.clock.today())
    out_json(r.to_dict()) if a.json else print_report(r)


def cmd_invoice(app: PosApp, a) -> None:
    store = app.directory.store(a.store) if a.store else None
    bill = app.repo.get_bill_by_invoice(a.invoice)
    if bill is None:
        if store is None:
            raise InvalidRequest("Invoice %s is not in this POS database; pass --store to read it from Shopify." % a.invoice)
        bill = app.refunds.find_bill(a.invoice, store)
    store = store or app.directory.store(bill.store_code)
    if a.html:
        with open(a.html, "w", encoding="utf-8") as f:
            f.write(render_html(bill, store))
        print("Wrote %s" % a.html)
    else:
        sys.stdout.write(render_text(bill, store))


def cmd_channels(app: PosApp, a) -> None:
    r = app.reports.channels(date.fromisoformat(a.date) if a.date else app.clock.today())
    if a.json:
        return out_json(r)
    for store, chans in r.items():
        print("%-4s %s" % (store, "  ".join("%s: %d bills, %s" % (ch, v["bills"], rs(v["net"])) for ch, v in chans.items()) or "no bills"))


def cmd_credit_note(app: PosApp, a) -> None:
    r = app.repo.get_refund_by_credit_note(a.credit_note)
    if r is None:
        raise InvalidRequest("No credit note %s in this POS." % a.credit_note)
    bill = app.repo.get_bill(r.bill_id)
    sys.stdout.write(render_credit_note_text(r, bill, app.directory.store(bill.store_code)))  # type: ignore[union-attr]


def cmd_audit(app: PosApp, a) -> None:
    for e in app.repo.audit_log(ref=a.ref, store_code=a.store, limit=a.limit):
        print("%s  %-8s %-24s %-22s %-18s %s" % (e["at"], e["actor"], e["action"], e["ref"] or "", e["reason_code"] or "",
                                                json.dumps(e["details"], ensure_ascii=False)))


def cmd_check_config(settings: Settings) -> int:
    cfg = BusinessConfig.load(settings.config_dir)
    print("Tax rates for HSN: %s" % ", ".join(cfg.taxes.hsns()))
    print("Offers loaded: %s" % ", ".join(o.offer_id for o in cfg.offers.offers))
    problems = cfg.problems()
    for where, why in problems:
        print("PROBLEM %s: %s" % (where, why))
    print("OK" if not problems else "%d problem(s)" % len(problems))
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eluno_pos", description="Eluno till: billing, refunds, remakes, cash till and day-end against Shopify.")
    p.add_argument("--base-url", help="mock / API base URL (default http://127.0.0.1:8787 or $ELUNO_POS_BASE_URL)")
    p.add_argument("--db", help="POS database file (default var/eluno_pos.db or $ELUNO_POS_DB)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("-v", "--verbose", action="store_true", help="log retries and HTTP behaviour")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("lookup", help="customer by phone + latest valid prescription")
    s.add_argument("--phone", required=True)

    for name, helptext in (("quote", "price a cart without saving it"), ("bill", "create a bill (and optionally pay it)")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("--store", required=True)
        s.add_argument("--staff", required=True)
        s.add_argument("--phone", required=True, help="bill-to customer")
        s.add_argument("--channel", default="walk_in", choices=["walk_in", "whatsapp_booked", "online_pickup"])
        s.add_argument("--pair", action="append", required=True, help="frame=SKU,lens=type/index/coating[,wearer=PHONE]; repeat per pair")
        s.add_argument("--discount", help="manager discount: pct=10,approver=S108,reason=WALKOUT_SAVE")
        s.add_argument("--ack", action="append", help="acknowledge a CONFIRM check by code (e.g. AXIS_OFF_GRID)")
        if name == "bill":
            s.add_argument("--pay", choices=["upi", "card", "cash"], help="take payment straight away")

    s = sub.add_parser("pay", help="take payment for a draft bill (safe to repeat)")
    s.add_argument("bill_id")
    s.add_argument("--method", required=True, choices=["upi", "card", "cash"])
    s.add_argument("--staff")

    s = sub.add_parser("cancel", help="abandon a draft, or a payment that never completed (releases its reserved frames)")
    s.add_argument("bill_id")
    s.add_argument("--staff", required=True)
    s.add_argument("--why", default="customer did not proceed")
    s.add_argument("--approver", help="store manager; required when the bill is stuck waiting on the terminal")

    s = sub.add_parser("void", help="return the money for a paid bill that cannot reach Shopify; the invoice stays as cancelled")
    s.add_argument("bill_id")
    s.add_argument("--staff", required=True)
    s.add_argument("--approver", required=True)
    s.add_argument("--reason", required=True)

    s = sub.add_parser("cutover", help="go-live: start each store's invoice/credit-note series after Shopify POS's last numbers")
    s.add_argument("--store", action="append", help="limit to these stores (repeatable)")

    sub.add_parser("sync", help="push paid bills and refunds that have not reached Shopify yet")

    s = sub.add_parser("refund", help="full refund, or partial with --lines")
    s.add_argument("invoice")
    s.add_argument("--store", required=True)
    s.add_argument("--staff", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--lines", help="comma-separated line refs (1F = pair 1 frame, 1L = pair 1 lens); default all")
    s.add_argument("--approver")
    s.add_argument("--note")

    s = sub.add_parser("remake", help="zero-charge lens remake against a parent invoice")
    s.add_argument("parent_invoice")
    s.add_argument("--store", required=True)
    s.add_argument("--staff", required=True)
    s.add_argument("--reason", required=True)
    s.add_argument("--cost-bearer", required=True)
    s.add_argument("--pair", type=int, default=1)
    s.add_argument("--lens", help="new lens type/index/coating (default: same lens)")
    s.add_argument("--ack", action="append")

    s = sub.add_parser("till", help="cash till session")
    s.add_argument("action", choices=["open", "paid-in", "paid-out", "close", "status"])
    s.add_argument("--store", required=True)
    s.add_argument("--staff", default="")
    s.add_argument("--amount", type=rupees_arg, help="rupees: float when opening, counted cash when closing")
    s.add_argument("--reason")
    s.add_argument("--note")

    s = sub.add_parser("day-end", help="day-end report for a store and date")
    s.add_argument("--store", required=True)
    s.add_argument("--date", help="YYYY-MM-DD (default today, IST)")

    s = sub.add_parser("channels", help="bills by sales channel, by store, for a date")
    s.add_argument("--date", help="YYYY-MM-DD (default today, IST)")

    s = sub.add_parser("credit-note", help="print a credit note")
    s.add_argument("credit_note")

    s = sub.add_parser("invoice", help="print an invoice (text, or --html FILE)")
    s.add_argument("invoice")
    s.add_argument("--store", help="needed only for invoices issued before this POS (read from Shopify)")
    s.add_argument("--html")

    s = sub.add_parser("audit", help="audit trail")
    s.add_argument("--ref")
    s.add_argument("--store")
    s.add_argument("--limit", type=int, default=50)

    sub.add_parser("check-config", help="validate tax rates, offers and reason codes")

    s = sub.add_parser("serve", help="run the HTTP API and till screen against --base-url")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    return p


COMMANDS = {"lookup": cmd_lookup, "quote": cmd_quote, "bill": cmd_bill, "pay": cmd_pay, "cancel": cmd_cancel, "void": cmd_void,
            "sync": cmd_sync, "cutover": cmd_cutover, "refund": cmd_refund, "remake": cmd_remake, "till": cmd_till, "day-end": cmd_day_end,
            "invoice": cmd_invoice, "audit": cmd_audit, "channels": cmd_channels, "credit-note": cmd_credit_note}


def main(argv: Optional[List[str]] = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252; invoices print ₹
    a = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="  %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env(base_url=a.base_url, db_path=a.db)
    if a.command == "check-config":
        return cmd_check_config(settings)
    if a.command == "serve":
        from .api import serve
        serve(settings, a.port, a.host)
        return 0
    app = PosApp(settings)
    try:
        COMMANDS[a.command](app, a)
        return 0
    except BillBlocked as e:
        print("BLOCKED: %s" % e.message, file=sys.stderr)
        for i in e.issues:
            print("  " + str(i), file=sys.stderr)
        return 2
    except DomainError as e:
        print("ERROR [%s]: %s" % (e.code, e.message), file=sys.stderr)
        return 2
    except IntegrationError as e:
        print("UPSTREAM ERROR: %s" % e, file=sys.stderr)
        return 3
    finally:
        app.close()
