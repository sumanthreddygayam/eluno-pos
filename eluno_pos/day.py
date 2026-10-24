"""One business day across all seven stores: these five orders are the only orders of the day.

    python run.py day        (also runs at the end of `python run.py demo`; skip it there with --no-day)

A fresh Shopify mock and a fresh POS database are used, so the day-end figures are exactly these five
orders. Each order runs end to end and mixes several cases. Every Must and Should item of the brief is
exercised somewhere in the day, and the run ends with a checklist that shows where, with the evidence
read back from Shopify, the terminal, the till and the POS database.

The clock labels (10:40, 14:00 …) are the story; system timestamps are real. Every step calls the same
services the CLI and the API use.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from typing import Dict, List

from .app import PosApp
from .app.billing import BillRequest, ManualDiscountRequest, PairRequest
from .cli import print_bill, print_quote, print_refund, print_report, print_till
from .config import Settings
from .demo import expect_error, orders_with_invoice, stock
from .devtools import FaultInjector, MockServer
from .domain.bill import Bill
from .domain.money import format_inr
from .invoice_render import render_credit_note_text, render_html, render_text

DAY = "2026-10-22"
STORES = ("MM", "HSR", "BR", "KMG", "JN", "IN", "VC")
MANAGERS = {"MM": "S104", "HSR": "S108", "BR": "S112", "KMG": "S116", "JN": "S120", "IN": "S124", "VC": "S128"}

REQUIREMENTS = [
    ("M1", "Must 1   Customer by phone (Shopify) + latest valid prescription (store tool)"),
    ("M2", "Must 2   Frame + lens from the matrix, lens validated against the Rx, actionable block"),
    ("M3", "Must 3   Offers by dates, conditions, stackability"),
    ("M4", "Must 4   GST per line by HSN: taxable, CGST, SGST, reconciled to the paisa"),
    ("M5", "Must 5   Charge, then Shopify order: lines, tax, discounts, sale txn, Rx on lens line, per-store invoice no."),
    ("M6", "Must 6   Full refund via Shopify: tax reversed per line, frame restocked at billing store, lens not"),
    ("M7", "Must 7   Day-end per store from Shopify: bills, gross, taxable, tax, discounts, refunds, payment split"),
    ("S1", "Should   Cash till: float, cash sales/refunds, paid-in/out with reasons, counted close, variance"),
    ("S2", "Should   Sales channel on every order + channel report by store"),
    ("S3", "Should   Partial refund (frame only or lens only)"),
    ("S4", "Should   Printable invoice (text and HTML) and credit note"),
    ("S5", "Should   Audit trail: who, what, when, reason code"),
    ("X1", "Stretch  Billing-time anomaly flags"),
    ("C1", "Constraint  429 / 503 handled; the same order is never created twice"),
    ("C2", "Constraint  Money is integer paise inside, decimal strings at the Shopify boundary"),
]
SHORT = {"M1": "Must 1 lookup", "M2": "Must 2 lens vs Rx", "M3": "Must 3 offers", "M4": "Must 4 GST", "M5": "Must 5 charge + Shopify order",
         "M6": "Must 6 full refund", "M7": "Must 7 day-end", "S1": "Should cash till", "S2": "Should channel", "S3": "Should partial refund",
         "S4": "Should printable invoice", "S5": "Should audit trail", "X1": "Stretch anomaly flags", "C1": "retries / no duplicates",
         "C2": "paise / decimal strings"}
EVIDENCE: Dict[str, List[str]] = {}


# ====================================================================== small helpers
def cover(req: str, where: str, evidence: str) -> None:
    EVIDENCE.setdefault(req, []).append("%s: %s" % (where, evidence))


def at(clock: str, title: str, covers=(), brief=()) -> None:
    print("\n" + "=" * 100)
    print("%s  %s" % (clock, title))
    for c in covers:
        print("       · " + c)
    if brief:
        print("       Brief: " + " | ".join(SHORT[r] for r in brief))
    print("-" * 100)


def note(text: str) -> None:
    print("  >> " + text)


def rs(paise: int) -> str:
    return "Rs %s" % format_inr(paise)


def lookup_line(billing, phone: str, where: str) -> None:
    """Must 1, shown the way the billing screen would: who, cohorts, which prescription and why."""
    v = billing.lookup(phone)
    c, rx = v["customer"], v["prescription"]
    skipped = ("; skipped " + ", ".join("%s (%s)" % (s["rx_id"], s["reason"].split(";")[0]) for s in v["skipped_prescriptions"])
               if v["skipped_prescriptions"] else "")
    print("  Lookup: %s %s %s · Rx %s (%s, valid until %s)%s" % (
        c["name"], c["phone"], " ".join("[%s]" % f for f in c["flags"]), rx["rx_id"] if rx else "NONE",
        rx["source"] if rx else "-", rx["valid_until"] if rx else "-", skipped))
    cover("M1", where, "%s -> %s%s" % (c["name"], rx["rx_id"] if rx else "no valid Rx", ", future-dated Rx skipped" if "after today" in skipped else ""))


def offers_evidence(q, where: str) -> None:
    applied = ["%s %s" % (d.offer_id, format_inr(d.amount)) for d in q.evaluation.applied]
    lost = [d for d in q.evaluation.decisions if not d.applied and "saves the customer more" in d.why]
    cover("M3", where, "applied %s%s" % (" + ".join(applied) or "none", "; lost: %s" % ", ".join(d.offer_id for d in lost) if lost else ""))


def gst_evidence(bill: Bill, where: str) -> None:
    parts = []
    for l in bill.lines:
        if l.taxable + l.cgst + l.sgst != l.gross:
            raise AssertionError("line %s does not reconcile" % l.line_ref)
        parts.append("%s HSN %s %s%%: %s + %s + %s = %s" % (l.line_ref, l.hsn, l.rate_bps // 100 if l.rate_bps % 100 == 0 else l.rate_bps / 100,
                                                         format_inr(l.taxable), format_inr(l.cgst), format_inr(l.sgst), format_inr(l.gross)))
    cover("M4", where, "; ".join(parts))


def shopify_order(url: str, invoice_no: str) -> dict:
    found = orders_with_invoice(url, invoice_no)
    return found[0] if found else {}


def shopify_state(url: str, invoice_no: str) -> str:
    found = orders_with_invoice(url, invoice_no) if invoice_no else []
    if not found:
        return "no order"
    o = found[0]
    return "%s %s%s" % (o["name"], o["financial_status"], "" if len(found) == 1 else " (DUPLICATES: %d)" % len(found))


def shopify_set_stock(url: str, item: int, location: int, available: int) -> None:
    """What another channel (website, HQ transfer) does to Shopify stock, outside the till."""
    body = json.dumps({"inventory_item_id": item, "location_id": location, "available": available}).encode()
    for _ in range(6):
        req = urllib.request.Request(url + "/admin/api/2026-10/inventory_levels/set.json", data=body, method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req).read()
            return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(1)


def timeline(app: PosApp, *refs: str) -> int:
    """Everything the audit trail recorded for these bills and refunds, in order."""
    events = sorted((e for r in refs if r for e in app.repo.audit_log(ref=r, limit=500)), key=lambda e: e["id"])
    print("  Timeline (audit trail):")
    shown = 0
    for e in events:
        if e["action"] == "bill.timing":
            continue
        shown += 1
        d = ", ".join("%s=%s" % (k, v) for k, v in e["details"].items() if v not in (None, [], {}, ""))
        print("    %s %-6s %-25s %s%s" % (e["at"][11:19], e["actor"], e["action"], (e["reason_code"] + " ") if e["reason_code"] else "", d[:120]))
    return shown


# ====================================================================== the day
def run(argv=()) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="    log: %(message)s")
    folder = tempfile.mkdtemp(prefix="eluno_pos_day_")
    print("\n" + "#" * 100)
    print("#  ONE BUSINESS DAY, %s: five orders are the only orders across all seven stores" % DAY)
    print("#  Fresh Shopify mock (429s and 503s on) and a fresh POS database, so the day-end is exactly these orders.")
    print("#" * 100)
    EVIDENCE.clear()
    with MockServer(flaky=True, today=DAY) as mock:
        faults = FaultInjector()
        app = PosApp(Settings(base_url=mock.url, db_path=os.path.join(folder, "pos.db"), business_date=DAY), transport=faults)
        print("Mock at %s · POS database and printed invoices in %s" % (mock.url, folder))
        try:
            simulate(app, mock.url, faults, folder)
        finally:
            app.close()
    print("\nDay simulation complete.")
    return 0


def simulate(app: PosApp, url: str, faults: FaultInjector, folder: str) -> None:
    b, today = app.billing, app.clock.today()
    orders: List[Dict] = []

    # ------------------------------------------------------------------ morning
    at("09:45", "Stores open: invoice series checked, every till opened with a Rs 3,000 float", brief=["S1"])
    for series, info in b.cutover().items():
        if info["floor"]:
            print("  %-20s next number after %05d (from Shopify POS history)" % (series, info["floor"]))
    for code in STORES:
        app.tills.open(code, MANAGERS[code], 300000)
    print("  Tills open at %s, each with a Rs 3,000 float (opened by the store manager)" % ", ".join(STORES))
    app.tills.move("VC", "S128", "paid_in", 50000, "CHANGE_FROM_HQ")
    print("  10:05 VC receives Rs 500 change from HQ (paid-in, reason CHANGE_FROM_HQ)")
    cover("S1", "09:45", "7 tills opened with a Rs 3,000 float; VC paid-in Rs 500 (CHANGE_FROM_HQ)")

    # ------------------------------------------------------------------ order 1
    at("10:40", "ORDER 1 · Koramangala (KMG) · Rajesh Pillai · progressive pair · UPI · booked on WhatsApp",
       ["the non-stackable campaign offer (PGX15) beats the stackable set",
        "terminal times out once (503) and Shopify is busy once (429): both retried safely",
        "16:30 the frame comes back with a loose hinge: frame-only refund, lens stays sold"],
       ["M1", "M2", "M3", "M4", "M5", "S2", "S3", "C1", "C2"])
    o1 = {"n": 1, "store": "KMG", "customer": "Rajesh Pillai", "channel": "whatsapp_booked", "bills": [], "refunds": [],
          "cases": "PGX15 beats the stack; 503 + 429 retried; frame-only refund later"}
    lookup_line(b, "+919000000013", "order 1")
    faults.outage("POST", "/terminal/charges", times=1)
    faults.reject("POST", "/orders.json", status=429, body={"errors": "Exceeded 2 calls per second for api client."}, times=1)
    bill, q = b.create(BillRequest("KMG", "S113", "+919000000013", "whatsapp_booked", (PairRequest("FR-0013", "progressive/1.67/anti_reflective"),)))
    print_quote(q)
    before = stock(url, 92000013, 70000004)
    bill = b.pay(bill.bill_id, "upi", "S113")
    print_bill(bill)
    for line in faults.log[-2:]:
        note("network: " + line)
    o = shopify_order(url, bill.invoice_no)
    lens = o["line_items"][1]
    props = {p["name"]: p["value"] for p in lens["properties"]}
    note("Shopify %s: invoice %s in note_attributes (Shopify's own number is %s); lens line has rx_id %s, lens_config %s; "
         "sale transaction %s %s" % (o["name"], bill.invoice_no, o["order_number"], props.get("rx_id"), props.get("lens_config"),
                                     o["transactions"][0]["gateway"], o["transactions"][0]["amount"]))
    note("FR-0013 at KMG in Shopify: %d -> %d" % (before, stock(url, 92000013, 70000004)))
    cover("M2", "order 1", "RX5020 (-3.00, ADD +2.50) fits progressive 1.67 anti-reflective")
    offers_evidence(q, "order 1")
    gst_evidence(bill, "order 1")
    cover("M5", "order 1", "charge %s, then %s with %d lines, tax_lines on each, PGX discount allocated to the lens, sale txn %s, rx_id %s on the lens line, "
          "invoice %s (not Shopify's %s)" % (bill.charge_id, o["name"], len(o["line_items"]), o["transactions"][0]["amount"],
                                             props.get("rx_id"), bill.invoice_no, o["name"]))
    cover("C1", "order 1", "terminal 503 and Shopify 429 injected, both retried; exactly %d order with %s" % (len(orders_with_invoice(url, bill.invoice_no)), bill.invoice_no))
    cover("C2", "order 1", "POS holds the frame at %d paise; Shopify got price %r and tax %r as strings" % (
        bill.lines[0].mrp, o["line_items"][0]["price"], o["line_items"][0]["tax_lines"][0]["price"]))
    cover("S2", "order 1", "channel whatsapp_booked on the order (note_attributes + tags)")
    o1["bills"].append(bill.bill_id)
    orders.append(o1)

    # ------------------------------------------------------------------ order 2
    at("11:15", "ORDER 2 · Jayanagar (JN) · Farhan Ali · frame + single vision · card declined, then cash · walk-in",
       ["priority customer: the stack (PRIORITY500 + BUNDLE300) beats FESTIVE10",
        "card declined: nothing charged, no invoice number used, frame stays reserved for him",
        "pays cash into the JN till; 18:00 changes his mind: full refund in cash with a credit note"],
       ["M1", "M3", "M4", "M5", "M6", "S1", "S4"])
    o2 = {"n": 2, "store": "JN", "customer": "Farhan Ali", "channel": "walk_in", "bills": [], "refunds": [],
          "cases": "card declined; paid cash; full cash refund later"}
    lookup_line(b, "+919000000009", "order 2")
    bill, q = b.create(BillRequest("JN", "S118", "+919000000009", "walk_in", (PairRequest("FR-0021", "single_vision/1.50/anti_reflective"),)))
    print_quote(q)
    faults.reject("POST", "/terminal/charges", status=402, body={"error": "card declined by issuer"}, times=1)
    expect_error(lambda: b.pay(bill.bill_id, "card", "S118"), "Farhan taps his card")
    d = b.get(bill.bill_id)
    note("bill is %s, invoice %s, charge %s, frame still reserved for Farhan: %d (until he pays another way or the bill is cancelled)" % (
        d.status.upper(), d.invoice_no, d.charge_id, sum(app.repo.holds([(92000021, 70000005)]).values())))
    bill = b.pay(bill.bill_id, "cash", "S118")
    print_bill(bill)
    note("JN till: float Rs 3,000 + this cash sale -> expected %s" % rs(app.tills.current("JN").expected_cash_paise))
    offers_evidence(q, "order 2")
    gst_evidence(bill, "order 2")
    cover("M5", "order 2", "declined card left no charge and no invoice number; cash bill %s -> %s (gateway cash)" % (
        bill.invoice_no, shopify_order(url, bill.invoice_no).get("name")))
    cover("S1", "order 2", "cash sale %s recorded in the JN till with the invoice number" % rs(bill.total))
    o2["bills"].append(bill.bill_id)
    orders.append(o2)

    # ------------------------------------------------------------------ order 3
    at("12:30", "ORDER 3 · Vega City (VC) · Harish Babu pays for himself and Rohit · two progressive pairs · UPI · online pickup",
       ["family billing: two wearers, two prescriptions, one bill",
        "two pairs unlock SECONDPAIR50; the manager asks for 10% (LOYALTY_GESTURE) but the stack saves more",
        "Wi-Fi drops after Shopify created the order: found by invoice number and adopted, no duplicate"],
       ["M1", "M2", "M3", "M4", "M5", "S2", "S4", "S5", "C1"])
    o3 = {"n": 3, "store": "VC", "customer": "Harish Babu (+ Rohit Jain)", "channel": "online_pickup", "bills": [], "refunds": [],
          "cases": "family, 2 pairs; SECONDPAIR50 stack; manager 10% loses; lost response adopted"}
    lookup_line(b, "+919000000025", "order 3")
    lookup_line(b, "+919000000029", "order 3 (wearer)")
    req = BillRequest("VC", "S125", "+919000000025", "online_pickup",
                      (PairRequest("FR-0026", "progressive/1.60/blue_cut"), PairRequest("FR-0016", "progressive/1.60/standard", "+919000000029")),
                      ManualDiscountRequest(1000, "S128", "LOYALTY_GESTURE"))
    bill, q = b.create(req)
    print_quote(q)
    faults.drop_response("POST", "/orders.json")
    bill = b.pay(bill.bill_id, "upi", "S125")
    print_bill(bill)
    o = shopify_order(url, bill.invoice_no)
    rx_on_lines = [{p["name"]: p["value"] for p in li["properties"]}.get("rx_id") for li in o["line_items"]]
    note("Shopify orders carrying %s: %d · Rx per lens line: %s · payer is customer %s" % (
        bill.invoice_no, len(orders_with_invoice(url, bill.invoice_no)), [r for r in rx_on_lines if r], o["customer"]["id"]))
    print(render_text(bill, app.directory.store("VC")))
    cover("M2", "order 3", "two wearers checked against their own prescriptions (RX5040 and RX5046)")
    offers_evidence(q, "order 3")
    gst_evidence(bill, "order 3")
    cover("M5", "order 3", "4 lines, Rx %s on the two lens lines, for_customer_id on Rohit's lines" % ", ".join(r for r in rx_on_lines if r))
    cover("C1", "order 3", "response of the order create lost after Shopify applied it; adopted, %d order" % len(orders_with_invoice(url, bill.invoice_no)))
    cover("S2", "order 3", "channel online_pickup")
    cover("S4", "order 3", "tax invoice printed above, family wearer shown on Rohit's line")
    o3["bills"].append(bill.bill_id)
    orders.append(o3)

    # ------------------------------------------------------------------ order 4
    at("14:00", "ORDER 4 · Mantri Mall (MM) · Varun Desai · single vision · stale quote, Shopify rejects, manager voids · walk-in",
       ["while Varun decides, HQ moves every FR-0010 out of MM: the draft can no longer be paid; it is cancelled",
        "Varun picks FR-0009 and pays by card; Shopify rejects the order (customer record disabled)",
        "the bill stays PAID with a valid invoice; a salesperson can't void it; the manager does (card refunded)",
        "the invoice number stays in the series as CANCELLED"],
       ["M1", "M3", "M5", "S5"])
    o4 = {"n": 4, "store": "MM", "customer": "Varun Desai", "channel": "walk_in", "bills": [], "refunds": [],
          "cases": "quote went stale; draft cancelled; Shopify 422; voided"}
    lookup_line(b, "+919000000015", "order 4")
    first, q = b.create(BillRequest("MM", "S101", "+919000000015", "walk_in", (PairRequest("FR-0010", "single_vision/1.56/blue_cut"),)))
    print_quote(q)
    shopify_set_stock(url, 92000010, 70000001, 0)
    note("meanwhile HQ transfers out every FR-0010 at MM (Shopify now says %d)" % stock(url, 92000010, 70000001))
    expect_error(lambda: b.pay(first.bill_id, "card", "S101"), "Varun pays for the FR-0010 bill")
    b.cancel(first.bill_id, "S101", "frame no longer in stock")
    note("first draft %s; MM invoice numbers used so far: %s" % (b.get(first.bill_id).status.upper(), app.repo.documents("MM", "invoice") or "none"))
    bill, q = b.create(BillRequest("MM", "S101", "+919000000015", "walk_in", (PairRequest("FR-0009", "single_vision/1.56/blue_cut"),)))
    faults.reject("POST", "/orders.json", status=422, body={"errors": {"customer": ["is disabled; enable the customer in Shopify admin"]}}, times=1000)
    bill = b.pay(bill.bill_id, "card", "S101")
    print_bill(bill)
    note("FR-0009 at MM: Shopify says %d, POS holds %d (Shopify was never told)" % (
        stock(url, 92000009, 70000001), sum(app.repo.holds([(92000009, 70000001)]).values())))
    expect_error(lambda: b.void(bill.bill_id, "S101", "S102", "SHOPIFY_REJECTED"), "a salesperson tries to approve the void")
    bill = b.void(bill.bill_id, "S101", "S104", "SHOPIFY_REJECTED")
    faults.clear()
    print_bill(bill)
    register = ["%s %s" % (x["number"], x["status"].upper()) for x in app.repo.documents("MM", "invoice")]
    note("card refunded on the terminal: %s · frame hold %d · MM invoice register: %s" % (
        bill.void_refund_id, sum(app.repo.holds([(92000009, 70000001)]).values()), register))
    offers_evidence(q, "order 4")
    cover("M5", "order 4", "stale quote refused before charging; Shopify 422 left the bill PAID; voided, number %s" % register[0])
    cover("S5", "order 4", "void refused for sales staff S102 (wrong_role); approved by manager S104 with reason SHOPIFY_REJECTED")
    o4["bills"] += [first.bill_id, bill.bill_id]
    orders.append(o4)

    # ------------------------------------------------------------------ order 5
    at("15:20", "ORDER 5 · HSR Layout (HSR) · Bhavana Shetty · progressive · UPI · Shopify lost mid-sale · walk-in",
       ["future-dated RX5036 ignored, RX5035 used; staff first picks a 1.56 lens: blocked, with the indices that fit",
        "first-time progressive and the salesperson has no progressive badge: flagged",
        "FESTIVE10 (Rs 299.80) vs BUNDLE300 (Rs 300.00): the customer gets the 20 paise better deal",
        "HSR has exactly one FR-0037: held while Shopify can't be reached",
        "Shopify creates the order, the answer is lost AND the lookup fails: bill stays PAID; evening sync adopts it",
        "17:45 the lab finds a scratch on the lens before handover: zero-charge remake, frame untouched"],
       ["M1", "M2", "M3", "M4", "M5", "X1", "C1"])
    o5 = {"n": 5, "store": "HSR", "customer": "Bhavana Shetty", "channel": "walk_in", "bills": [], "refunds": [],
          "cases": "future Rx skipped; 1.56 blocked; 20-paise offer choice; crash recovery by adoption; remake"}
    lookup_line(b, "+919000000022", "order 5")
    expect_error(lambda: b.create(BillRequest("HSR", "S106", "+919000000022", "walk_in", (PairRequest("FR-0037", "progressive/1.56/standard"),))),
                 "staff tries a progressive 1.56")
    bill, q = b.create(BillRequest("HSR", "S106", "+919000000022", "walk_in", (PairRequest("FR-0037", "progressive/1.67/standard"),)))
    print_quote(q)
    faults.drop_response("POST", "/orders.json")
    faults.outage("GET", "/orders.json")
    bill = b.pay(bill.bill_id, "upi", "S106")
    faults.clear()
    print_bill(bill)
    note("truth in Shopify right now: %d order(s) with %s (the POS can't know yet); FR-0037 at HSR: Shopify %d, POS hold %d" % (
        len(orders_with_invoice(url, bill.invoice_no)), bill.invoice_no, stock(url, 92000037, 70000002),
        sum(app.repo.holds([(92000037, 70000002)]).values())))
    cover("M2", "order 5", "progressive 1.56 blocked for RX5035 (-5.75) with 'Indices that fit: 1.60, 1.67, 1.74'; 1.67 billed")
    cover("X1", "order 5", ", ".join(i.code for i in q.issues if i.severity != "block") + " (S106 has no progressive_expert badge)")
    offers_evidence(q, "order 5")
    gst_evidence(bill, "order 5")
    o5["bills"].append(bill.bill_id)
    orders.append(o5)

    # ------------------------------------------------------------------ afternoon / evening
    at("16:30", "ORDER 1 continued · loose hinge: frame-only refund at KMG", brief=["S3", "S4"])
    r1 = app.refunds.refund(b.get(o1["bills"][0]).invoice_no, "KMG", "S113", "DEFECTIVE_FRAME", ["1F"])
    print_refund(r1)
    note("FR-0013 back on the KMG shelf: Shopify %d; order is now %s" % (stock(url, 92000013, 70000004), shopify_state(url, r1.invoice_no)))
    cover("S3", "16:30 order 1", "frame line 1F refunded (%s, tax reversed %s), lens kept; Shopify %s" % (
        rs(r1.amount), rs(r1.tax), shopify_state(url, r1.invoice_no)))
    o1["refunds"].append(r1.refund_id)

    at("17:00", "Evening sync: push everything still waiting for Shopify", brief=["C1"])
    for line in app.sync_pending():
        print("  " + line)
    bill5 = b.get(o5["bills"][0])
    note("order 5 is %s as %s; Shopify orders with %s: %d (adopted, not duplicated); hold %d" % (
        bill5.status.upper(), bill5.shopify_order_name, bill5.invoice_no, len(orders_with_invoice(url, bill5.invoice_no)),
        sum(app.repo.holds([(92000037, 70000002)]).values())))
    cover("C1", "17:00 order 5", "order landed but its answer was lost and the lookup failed; evening sync adopted %s, %d order" % (
        bill5.shopify_order_name, len(orders_with_invoice(url, bill5.invoice_no))))
    cover("M5", "order 5", "paid and invoiced while Shopify was unreachable, synced at 17:00 as %s" % bill5.shopify_order_name)

    at("17:45", "ORDER 5 continued · lens scratched at fitting: remake at no charge")
    remake = app.remakes.remake(bill5.invoice_no, "HSR", "S106", "LAB_DEFECT", "eluno")
    print_bill(remake)
    note("FR-0037 at HSR still %d (frame not re-billed or re-reserved)" % stock(url, 92000037, 70000002))
    o5["bills"].append(remake.bill_id)

    at("18:00", "ORDER 2 continued · Farhan returns: full refund in cash from the JN till", brief=["M6", "S1", "S4"])
    app.tills.move("JN", "S120", "paid_out", 12000, "COURIER")
    print("  17:50 JN pays Rs 120 to the courier (paid-out, reason COURIER)")
    jn_before = stock(url, 92000021, 70000005)
    bill2 = b.get(o2["bills"][0])
    r2 = app.refunds.refund(bill2.invoice_no, "JN", "S118", "CHANGED_MIND")
    print_refund(r2)
    print(render_credit_note_text(r2, bill2, app.directory.store("JN")))
    o = shopify_order(url, bill2.invoice_no)
    rf = o["refunds"][0]
    note("Shopify %s is %s; refund lines %s; refund transaction parent %s = sale transaction %s; FR-0021 at JN %d -> %d" % (
        o["name"], o["financial_status"], [(x["restock_type"], x["total_tax"]) for x in rf["refund_line_items"]],
        rf["transactions"][0]["parent_id"], o["transactions"][0]["id"], jn_before, stock(url, 92000021, 70000005)))
    cover("M6", "18:00 order 2", "%s %s; frame 'return' at JN (stock %d -> %d), lens 'no_restock'; tax reversed %s; credit note %s; refund against sale txn %s" % (
        o["name"], o["financial_status"], jn_before, stock(url, 92000021, 70000005), rs(r2.tax), r2.credit_note_no, o["transactions"][0]["id"]))
    cover("S1", "18:00", "JN paid-out Rs 120 (COURIER); cash refund %s out of the JN till" % rs(r2.amount))
    cover("S4", "18:00", "credit note %s printed against %s" % (r2.credit_note_no, bill2.invoice_no))
    o2["refunds"].append(r2.refund_id)

    at("21:00", "Stores close: every till counted", brief=["S1"])
    counted = {code: app.tills.current(code).expected_cash_paise for code in STORES}
    counted["VC"] -= 30000  # VC is Rs 300 short
    for code in STORES:
        print_till(app.tills.close(code, MANAGERS[code], counted[code]))
    cover("S1", "21:00", "7 tills closed with counts; VC Rs 300 short -> variance flagged to finance (over Rs 200)")

    # ------------------------------------------------------------------ analysis
    at("21:15", "THE FIVE ORDERS: outcome of each", brief=["S5"])
    print("  %-2s %-4s %-26s %-15s %-35s %-12s %12s %12s %12s  %s" % ("#", "Store", "Customer", "Channel", "Invoices (final status)", "Credit notes",
                                                                       "Charged", "Refunded", "Kept", "Shopify"))
    print("  " + "-" * 175)
    for o in orders:
        bills = [b.get(x) for x in o["bills"]]
        refunds = [app.repo.get_refund(x) for x in o["refunds"]]
        charged = sum(x.total for x in bills if x.status in ("paid", "synced", "voided"))
        refunded = sum(r.amount for r in refunds) + sum(x.total for x in bills if x.status == "voided")
        invs = ", ".join("%s %s" % ((x.invoice_no or "draft")[-8:], x.status) for x in bills)
        shop = "; ".join(shopify_state(url, x.invoice_no) for x in bills if x.invoice_no)
        print("  %-2d %-4s %-26s %-15s %-35s %-12s %12s %12s %12s  %s" % (
            o["n"], o["store"], o["customer"][:26], o["channel"], invs[:35], ", ".join(r.credit_note_no[-8:] for r in refunds) or "-",
            format_inr(charged), format_inr(refunded), format_inr(charged - refunded), shop))
        print("     cases: %s" % o["cases"])
    events = 0
    for o in orders:
        print("\n  Order %d (%s):" % (o["n"], o["customer"]))
        events += timeline(app, *(o["bills"] + o["refunds"]))
    cover("S5", "21:15", "%d audit events for the five orders, each with actor, time and reason code where one applies" % events)

    for x in [b.get(i) for o in orders for i in o["bills"]]:
        if x.invoice_no:
            with open(os.path.join(folder, x.invoice_no.replace("/", "_") + ".html"), "w", encoding="utf-8") as f:
                f.write(render_html(x, app.directory.store(x.store_code)))
    cover("S4", "21:15", "HTML invoice for every invoice written to %s" % folder)

    at("21:30", "DAY-END: every store (from Shopify, reconciled with the tills and the POS ledger)", brief=["M7"])
    reports = {code: app.reports.day_end(code, today) for code in STORES}
    for code in STORES:
        if reports[code].bills or reports[code].refunds_count or reports[code].findings:
            print_report(reports[code])
            print()
    quiet = [c for c in STORES if not (reports[c].bills or reports[c].refunds_count or reports[c].findings)]
    if quiet:
        print("  No bills today at: %s (tills opened and closed with the float)" % ", ".join(quiet))

    at("21:40", "ALL STORES, %s" % today, brief=["M7", "S2"])
    cols = ("Store", "Bills", "Gross MRP", "Discounts", "Net sales", "Taxable", "GST", "Refunds", "Net after", "Cash", "UPI", "Card", "Till var.")
    print("  %-5s %5s %12s %10s %12s %12s %10s %11s %12s %10s %11s %10s %9s" % cols)
    total = {k: 0 for k in ("bills", "gross", "disc", "net", "taxable", "tax", "ref", "after", "cash", "upi", "card")}
    for code in STORES:
        r = reports[code]
        m = {k: (r.by_payment_method[k].net if k in r.by_payment_method else 0) for k in ("cash", "upi", "card")}
        row = {"bills": r.bills, "gross": r.gross_mrp, "disc": r.discounts, "net": r.net_sales, "taxable": r.taxable, "tax": r.tax,
               "ref": r.refunds_amount, "after": r.net_after_refunds, **m}
        for k in total:
            total[k] += row[k]
        var = r.till["variance"] if r.till and r.till["variance"] is not None else 0
        print("  %-5s %5d %12s %10s %12s %12s %10s %11s %12s %10s %11s %10s %9s" % (
            code, row["bills"], format_inr(row["gross"]), format_inr(row["disc"]), format_inr(row["net"]), format_inr(row["taxable"]),
            format_inr(row["tax"]), format_inr(row["ref"]), format_inr(row["after"]), format_inr(row["cash"]), format_inr(row["upi"]),
            format_inr(row["card"]), format_inr(var) + (" !" if r.till and r.till["flagged"] else "")))
    print("  " + "-" * 140)
    print("  %-5s %5d %12s %10s %12s %12s %10s %11s %12s %10s %11s %10s" % (
        "TOTAL", total["bills"], format_inr(total["gross"]), format_inr(total["disc"]), format_inr(total["net"]), format_inr(total["taxable"]),
        format_inr(total["tax"]), format_inr(total["ref"]), format_inr(total["after"]), format_inr(total["cash"]), format_inr(total["upi"]),
        format_inr(total["card"])))
    if total["after"] != total["cash"] + total["upi"] + total["card"]:
        raise AssertionError("payment split does not add up to net after refunds")
    print("\n  Channel by store (Retail's ask):")
    for code, chans in app.reports.channels(today).items():
        print("    %-4s %s" % (code, "  ".join("%s %d bill(s) net %s" % (ch, v["bills"], format_inr(v["net"])) for ch, v in chans.items()) or "-"))
    print("  Discounts by offer: %s" % ", ".join("%s %s" % (k, format_inr(v)) for k, v in sorted(_sum_offers(reports).items())))
    print("  Documents issued today: %s" % "; ".join("%s %s" % (d["number"], d["status"]) for code in STORES for d in app.repo.documents(code)
                                                     if not d["number"].startswith(("PO-", "PI-"))))
    cover("M7", "21:30", "7 stores from Shopify orders: %d bills, gross %s, discounts %s, net %s, taxable %s, GST %s, refunds %s; "
          "split cash %s / UPI %s / card %s = net after refunds %s" % (
              total["bills"], rs(total["gross"]), rs(total["disc"]), rs(total["net"]), rs(total["taxable"]), rs(total["tax"]), rs(total["ref"]),
              rs(total["cash"]), rs(total["upi"]), rs(total["card"]), rs(total["after"])))
    cover("S2", "21:40", "channel report by store: %s" % ", ".join("%s %d" % kv for kv in sorted(_sum_channels(reports).items())))

    at("10:00 next day", "The founder's morning email (Rohan): yesterday's bills, average bill and discount, by store")
    print("  %-5s %6s %14s %14s" % ("Store", "Bills", "Average bill", "Discount"))
    for code in STORES:
        r = reports[code]
        print("  %-5s %6d %14s %14s" % (code, r.bills, format_inr(r.average_bill), format_inr(r.discounts)))
    paid = sum(r.bills - r.zero_value_bills for r in reports.values())
    print("  %-5s %6d %14s %14s" % ("ALL", total["bills"], format_inr(total["net"] // paid if paid else 0), format_inr(total["disc"])))
    print("  (computed from the same day-end data; sending the email is not built)")

    at("Checklist", "Every Must and Should item of the brief, and where today's orders exercised it")
    for code, label in REQUIREMENTS:
        items = EVIDENCE.get(code, [])
        print("  %s %s" % ("[x]" if items else "[ ]", label))
        for e in items:
            print("        - " + e[:200])
    missing = [c for c, _ in REQUIREMENTS if c not in EVIDENCE]
    print("\n  %d of %d covered%s" % (len(REQUIREMENTS) - len(missing), len(REQUIREMENTS), "; missing: %s" % missing if missing else "."))


def _sum_channels(reports) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in reports.values():
        for ch, m in r.by_channel.items():
            out[ch] = out.get(ch, 0) + m.bills
    return out


def _sum_offers(reports) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in reports.values():
        for k, v in r.discounts_by_offer.items():
            out[k] = out.get(k, 0) + v
    return out
