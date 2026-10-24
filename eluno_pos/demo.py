"""End-to-end walkthrough against a fresh, flaky mock (429s and 503s on) with injected faults.

    python run.py demo            business date pinned to 2026-10-22 (the exercise's data date) for reproducible output
    python run.py demo --live     use today's real date (offers and prescriptions then age with the calendar)

Every step calls the same application services the CLI and API use.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
import urllib.request
from dataclasses import replace
from typing import Callable

from .app import PosApp
from .app.billing import BillRequest, ManualDiscountRequest, PairRequest
from .cli import print_bill, print_quote, print_refund, print_report, print_till
from .config import ROOT, Settings
from .devtools import FaultInjector, MockServer
from .domain.errors import BillBlocked, DomainError
from .invoice_render import render_credit_note_text, render_text

STEP = [0]


def step(title: str, why: str = "") -> None:
    STEP[0] += 1
    print("\n" + "=" * 100)
    print("%2d. %s" % (STEP[0], title))
    if why:
        print("    " + why)
    print("-" * 100)


def expect_error(fn: Callable, label: str) -> None:
    try:
        fn()
    except BillBlocked as e:
        print("  %s -> BLOCKED: %s" % (label, e.message))
        for i in e.issues:
            print("     " + str(i))
        return
    except DomainError as e:
        print("  %s -> refused [%s]: %s" % (label, e.code, e.message))
        return
    print("  %s -> (unexpectedly allowed: the data has aged past the demo date; run without --live)" % label)


def shopify_get(url: str, path: str) -> dict:
    with urllib.request.urlopen("%s/admin/api/2026-10/%s" % (url, path)) as r:
        return json.loads(r.read())


def stock(url: str, item: int, loc: int) -> int:
    return shopify_get(url, "inventory_levels.json?inventory_item_ids=%d&location_ids=%d" % (item, loc))["inventory_levels"][0]["available"]


def orders_with_invoice(url: str, invoice_no: str) -> list:
    return [o for o in shopify_get(url, "orders.json?status=any&limit=250")["orders"]
            if any(a["name"] == "invoice_no" and a["value"] == invoice_no for a in o["note_attributes"])]


def pay(app: PosApp, req: BillRequest, method: str):
    bill, q = app.billing.create(req)
    print_quote(q)
    t = time.monotonic()
    bill = app.billing.pay(bill.bill_id, method, req.staff_id)
    print_bill(bill)
    print("  checkout (charge + invoice number + Shopify order, incl. retries): %.2fs" % (time.monotonic() - t))
    return bill


def lookup_all(billing) -> None:
    """Step 2 for every seed customer: flags, the prescription the till would use, and why others were skipped."""
    with open(os.path.join(ROOT, "starter-kit", "data", "customers.json"), encoding="utf-8") as f:
        phones = [c["phone"] for c in json.load(f)]
    row = "  %-17s %-14s %-42s %-29s %-27s %s"
    print(row % ("Customer", "Phone", "Flags", "Rx used (valid until)", "Checks", "Skipped (why)"))
    print("  " + "-" * 160)
    counts = {"valid": 0, "none": 0}
    for phone in phones:
        v = billing.lookup(phone)
        c, rx = v["customer"], v["prescription"]
        counts["valid" if rx else "none"] += 1
        used = "%s %s (%s)" % (rx["rx_id"], rx["source"].replace("_", "-"), rx["valid_until"]) if rx else "NONE - lens sale blocked"
        skipped = ", ".join("%s %s" % (s["rx_id"], "future-dated" if "after today" in s["reason"] else s["reason"].replace("expired on ", "exp "))
                            for s in v["skipped_prescriptions"]) or "-"
        checks = ", ".join(f["code"] for f in v["flags"]) or "-"
        print(row % (c["name"][:17], c["phone"], " ".join(c["flags"]) or "-", used, checks, skipped))
    print("\n  %d customers: %d with a usable prescription, %d without (frames and sunglasses only until re-tested)."
          % (len(phones), counts["valid"], counts["none"]))


DEMO_DATE = "2026-10-22"


def run(argv=()) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="    log: %(message)s")
    db = os.path.join(tempfile.mkdtemp(prefix="eluno_pos_demo_"), "pos.db")
    today = None if "--live" in argv else DEMO_DATE
    with MockServer(flaky=True, today=today) as mock:
        faults = FaultInjector()
        app = PosApp(Settings(base_url=mock.url, db_path=db, business_date=today), transport=faults)
        print("Mock (flaky: ~10%% of Shopify writes 429, terminal 503s once per new order_ref) at %s\nPOS database: %s\nBusiness date: %s" % (
            mock.url, db, app.clock.today()))
        try:
            _scenario(app, mock.url, faults)
        finally:
            app.close()
        http = app.http
        print("\nHTTP calls: %s" % ", ".join("%s %d (retries %d)" % (k, c.calls, c.retries) for k, c in http.items()))
    print("\nDemo complete.")
    if "--no-day" not in argv:
        from .day import run as run_day  # part 2: one business day, five orders across all stores
        run_day(argv)
        from .race import run as run_race  # part 3: two tills, one last frame
        run_race(argv)
    return 0


def _scenario(app: PosApp, url: str, faults: FaultInjector) -> None:
    b = app.billing

    step("Cutover: each store's invoice series continues after Shopify POS's last number (gapless, per store)")
    for series, info in app.billing.cutover().items():
        if info["floor"]:
            print("  %-22s continues after %05d" % (series, info["floor"]))

    step("Customer lookup: Shopify customer by phone + latest valid prescription from the store tool",
         "Priority and campaign cohorts are flagged on the billing screen, same as the queue tool.")
    v = b.lookup("90000 00005")
    print(json.dumps({"customer": v["customer"], "prescription": v["prescription"]["rx_id"]}, indent=1))
    print("\n  Every customer in the seed (starter-kit/data/customers.json), looked up the same way:\n")
    lookup_all(b)

    step("Lens validated against the prescription",
         "Sanjay is -5.75: a 1.56 progressive is blocked with a reason and the indices that would fit.")
    req = BillRequest("BR", "S111", "+919000000021", "walk_in", (PairRequest("FR-0007", "progressive/1.56/blue_cut"),))
    expect_error(lambda: b.create(req), "Sanjay, 1.56 progressive")
    expect_error(lambda: b.create(BillRequest("HSR", "S105", "+919000000012", "walk_in", (PairRequest("FR-0007", "single_vision/1.50/standard"),))),
                 "Kavya, no valid Rx")
    expect_error(lambda: b.create(BillRequest("HSR", "S105", "+919000000001", "walk_in", (PairRequest("FR-0007", "bifocal/1.50/standard"),))),
                 "Rahul, bifocal without ADD")
    expect_error(lambda: b.create(BillRequest("JN", "S117", "+919000000003", "walk_in", (PairRequest("FR-0005", "single_vision/1.56/standard"),))),
                 "Suresh, external Rx with axis 87")

    step("Offers + GST + charge + Shopify order (Karthik, progressive campaign + priority customer, HSR)",
         "Non-stackable PGX15 (Rs 1,424.85) loses to the stackable set (Rs 1,800). Invoice series continues after Shopify POS's last HSR number.")
    before = stock(url, 92000003, 70000002)
    karthik = pay(app, BillRequest("HSR", "S105", "+919000000005", "whatsapp_booked", (PairRequest("FR-0003", "progressive/1.60/blue_cut"),)), "upi")
    print("  FR-0003 stock at HSR: %d -> %d" % (before, stock(url, 92000003, 70000002)))
    o = orders_with_invoice(url, karthik.invoice_no)[0]
    lens = o["line_items"][1]
    print("  Shopify %s: total %s, tax %s, lens tax_lines %s, Rx on lens line: %s" % (
        o["name"], o["total_price"], o["total_tax"], [(t["title"], t["price"]) for t in lens["tax_lines"]],
        [p["value"] for p in lens["properties"] if p["name"] == "rx_id"]))
    print()
    print(render_text(karthik, app.directory.store("HSR")))

    step("Wi-Fi drop mid-bill: Shopify creates the order but the response is lost",
         "The adapter looks the invoice number up before retrying and adopts the order; no duplicate.")
    faults.drop_response("POST", "/orders.json", times=1)
    sanjay = pay(app, BillRequest("BR", "S111", "+919000000021", "walk_in", (PairRequest("FR-0007", "progressive/1.60/blue_cut"),)), "card")
    print("  fault log: %s" % faults.log[-1])
    print("  orders in Shopify with %s: %d" % (sanjay.invoice_no, len(orders_with_invoice(url, sanjay.invoice_no))))
    again = b.pay(sanjay.bill_id, "card")
    print("  till repeats 'pay' after reconnecting -> %s, still %d order(s), terminal charge %s" % (
        again.status, len(orders_with_invoice(url, sanjay.invoice_no)), again.charge_id))

    step("Family billing: one bill, two wearers, two prescriptions (IN)",
         "Each lens line carries its own Rx and wearer. Two pairs unlock SECONDPAIR50 on the cheaper lens.")
    expect_error(lambda: b.create(BillRequest("IN", "S121", "+919000000007", "walk_in",
                                              (PairRequest("FR-0001", "progressive/1.50/standard"),
                                               PairRequest("FR-0005", "single_vision/1.50/anti_reflective", "+919000000010")))), "frame out of stock at IN")
    family = pay(app, BillRequest("IN", "S121", "+919000000007", "walk_in",
                                  (PairRequest("FR-0001", "progressive/1.50/standard"),
                                   PairRequest("FR-0014", "single_vision/1.50/anti_reflective", "+919000000010"))), "upi")

    step("Manager discount for a walk-out: needs a store manager and a reason code; both are audited")
    walkout = BillRequest("HSR", "S106", "+919000000001", "walk_in", (PairRequest("FR-0007", "single_vision/1.67/anti_reflective"),),
                          ManualDiscountRequest(1000, "S106", "WALKOUT_SAVE"))
    expect_error(lambda: b.create(walkout), "sales staff approving their own discount")
    expect_error(lambda: b.create(replace(walkout, manual_discount=ManualDiscountRequest(1500, "S108", "WALKOUT_SAVE"))), "15% (over the 10% cap)")
    expect_error(lambda: b.create(replace(walkout, manual_discount=ManualDiscountRequest(1000, "S108", ""))), "no reason code")
    walkout = replace(walkout, manual_discount=ManualDiscountRequest(1000, "S108", "WALKOUT_SAVE"))

    step("Shopify outage at the moment of sale: payment taken, invoice issued, order queued (outbox)",
         "The customer leaves with a valid invoice; sync pushes the order when Shopify is back.")
    fr7 = (92000007, 70000002)  # FR-0007 at HSR: the only one in the store
    print("  FR-0007 at HSR before the sale: Shopify says %d" % stock(url, *fr7))
    faults.outage("POST", "/orders.json")
    faults.outage("GET", "/orders.json")
    rahul = pay(app, walkout, "upi")
    print("  FR-0007 at HSR now: Shopify still says %d (it hasn't been told), POS holds %d for this bill" % (
        stock(url, *fr7), app.repo.holds([fr7]).get(fr7, 0)))
    expect_error(lambda: b.create(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0007"),))),
                 "another till tries to sell the same FR-0007 meanwhile")
    faults.clear()
    print("  Shopify is back. Before sync, day-end reconciliation shows:")
    for f in app.reports.day_end("HSR", app.clock.today()).findings:
        print("    [%s] %s" % (f.severity.upper(), f.message[:160]))
    print("  sync -> %s" % app.sync_pending())
    print("  orders in Shopify with %s: %d" % (rahul.invoice_no, len(orders_with_invoice(url, rahul.invoice_no))))
    print("  FR-0007 at HSR after sync: Shopify says %d, POS hold %d (released: Shopify's count has the sale now)" % (
        stock(url, *fr7), app.repo.holds([fr7]).get(fr7, 0)))

    step("Two customers want the last frame at the same time (HSR has one FR-0005)",
         "The first customer's payment holds the frame; the second till is refused before anything is charged.")
    fr5 = (92000005, 70000002)
    gaurav, _ = b.create(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0005"),)))
    sunita, _ = b.create(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)))
    print("  Both bills created as drafts (Shopify says %d in stock). Gaurav pays first, but his card machine hangs:" % stock(url, *fr5))
    faults.outage("POST", "/terminal/charges")
    expect_error(lambda: b.pay(gaurav.bill_id, "card"), "Gaurav pays by card")
    print("  Gaurav's bill is %s; the POS holds %d FR-0005 for it" % (b.get(gaurav.bill_id).status.upper(), app.repo.holds([fr5]).get(fr5, 0)))
    expect_error(lambda: b.pay(sunita.bill_id, "upi"), "Sunita pays for the same frame at the other till")
    print("  Sunita's bill: %s, charge %s (nothing taken)" % (b.get(sunita.bill_id).status.upper(), b.get(sunita.bill_id).charge_id))
    faults.clear()
    done = b.pay(gaurav.bill_id, "card")
    print("  Card machine back; Gaurav pays again (same bill reference) -> %s %s; Shopify FR-0005 at HSR: %d; hold %d" % (
        done.status.upper(), done.invoice_no, stock(url, *fr5), app.repo.holds([fr5]).get(fr5, 0)))
    expect_error(lambda: b.pay(sunita.bill_id, "upi"), "Sunita tries again")

    step("Cash bill with an open till; axis off the 5-degree grid acknowledged by staff (JN)")
    app.tills.open("JN", "S120", 300000)
    suresh_req = BillRequest("JN", "S117", "+919000000003", "walk_in", (PairRequest("FR-0005", "single_vision/1.56/standard"),), acknowledge=("AXIS_OFF_GRID",))
    pay(app, suresh_req, "cash")
    app.tills.move("JN", "S120", "paid_out", 12000, "COURIER")
    app.tills.move("JN", "S120", "paid_out", 8000, "TEA_SNACKS")
    app.tills.move("JN", "S120", "paid_in", 100000, "CHANGE_FROM_HQ")

    step("Full refund (Karthik): tax reversed per line, frame back to HSR stock, lens not restocked, credit note",
         "Terminal refund first, then the credit note number, then Shopify's refund against the sale transaction.")
    before = stock(url, 92000003, 70000002)
    print_refund(app.refunds.refund(karthik.invoice_no, "HSR", "S105", "CHANGED_MIND"))
    o = orders_with_invoice(url, karthik.invoice_no)[0]
    print("  FR-0003 stock at HSR: %d -> %d;  Shopify financial_status: %s" % (before, stock(url, 92000003, 70000002), o["financial_status"]))
    expect_error(lambda: app.refunds.refund(karthik.invoice_no, "HSR", "S105", "CHANGED_MIND"), "refund it again")

    step("Partial refund (family bill): second pair's frame only, with its credit note")
    partial = app.refunds.refund(family.invoice_no, "IN", "S121", "DEFECTIVE_FRAME", ["2F"])
    print_refund(partial)
    print(render_credit_note_text(partial, app.billing.get(family.bill_id), app.directory.store("IN")))

    step("Refund of a bill made before cutover (Shopify POS, cash): read from Shopify, refunded through the JN till")
    print_refund(app.refunds.refund("ELU/JN/FY26/00002", "JN", "S117", "CHANGED_MIND"))

    step("Remake: progressive non-adaptation. Lens only, zero charge, frame not re-billed or re-reserved")
    before = stock(url, 92000007, 70000003)
    remake = app.remakes.remake(sanjay.invoice_no, "BR", "S111", "PROGRESSIVE_NON_ADAPTATION", "eluno")
    print_bill(remake)
    print("  FR-0007 stock at BR unchanged: %d -> %d" % (before, stock(url, 92000007, 70000003)))
    print(render_text(remake, app.directory.store("BR")))

    step("Close the JN till: counted Rs 250 short -> variance flagged to finance")
    s = app.tills.current("JN")
    print_till(app.tills.close("JN", "S120", s.expected_cash_paise - 25000))

    step("Day-end reports (from Shopify orders, reconciled with the POS ledger and the till)")
    for store in ("HSR", "JN", "IN"):
        print_report(app.reports.day_end(store, app.clock.today()))
        print()

    step("Where bills came from today: channel by store (Retail's ask)")
    for store, chans in app.reports.channels(app.clock.today()).items():
        if chans:
            print("  %-4s %s" % (store, "  ".join("%s: %d bill(s), net Rs %d.%02d" % ((ch, v["bills"]) + divmod(v["net"], 100)) for ch, v in chans.items())))

    step("Audit trail for the walk-out discount bill")
    for e in app.repo.audit_log(ref=rahul.bill_id):
        print("  %s %-7s %-24s %s %s" % (e["at"][11:19], e["actor"], e["action"], e["reason_code"] or "", json.dumps(e["details"])))
