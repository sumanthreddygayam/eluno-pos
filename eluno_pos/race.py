"""Two tills, one last frame: what each till sees, second by second.

    python run.py race       (also runs at the end of `python run.py demo`)

Two separate POS processes (two `PosApp`s, two database connections) share one store database and one
Shopify, like two counters at Bel Road. Till 1 starts paying for the store's only unit of a frame; one
second later Till 2 tries to sell the same frame. Four cases:

  A  Till 1's payment is slow but succeeds        -> Till 2 sees it as held, then as sold out
  B  Till 1's customer returns the frame          -> it is back on the shelf; Till 2 sells it
  C  Till 1's card is declined                    -> the frame stays reserved until Till 1 cancels the bill
  D  Till 1's terminal stops answering            -> reserved; only a store manager can cancel; then it is free

A failed or unfinished payment never appears in Shopify's orders, and its frame never becomes sellable
until the bill is completed or cancelled.
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .app import PosApp
from .app.billing import BillRequest, PairRequest
from .config import Settings
from .demo import orders_with_invoice, shopify_get, stock
from .devtools import FaultInjector, MockServer
from .domain.errors import BillBlocked, DomainError

DAY = "2026-10-22"
BR = 70000003  # Bel Road


class Clock0:
    def __init__(self):
        self.t0 = time.monotonic()

    def reset(self) -> None:
        self.t0 = time.monotonic()

    def say(self, who: str, text: str) -> None:
        print("  t+%4.1fs  %-7s %s" % (time.monotonic() - self.t0, who, text))


def attempt(fn: Callable) -> Tuple[Optional[object], str]:
    """Run a till action; return (result, what the till screen would say)."""
    try:
        return fn(), ""
    except BillBlocked as e:
        return None, "BLOCKED: " + "; ".join("%s %s" % (i.code, i.message) for i in e.issues)
    except DomainError as e:
        return None, "REFUSED [%s]: %s" % (e.code, e.message)


class Board:
    """The 'list' both tills look at: the frame's stock and the orders that exist."""

    def __init__(self, url: str, app: PosApp, labels: Dict[str, str]):
        self.url, self.app, self.labels = url, app, labels

    def show(self, sku: str, item: int, title: str) -> None:
        in_shopify = stock(self.url, item, BR)
        holds = [h for h in self.app.repo.hold_list(BR) if h["inventory_item_id"] == item]
        held = sum(h["qty"] for h in holds)
        print("      ┌ %s" % title)
        print("      │ %s at Bel Road: Shopify %d · reserved by POS %d%s · AVAILABLE TO SELL %d" % (
            sku, in_shopify, held, (" (%s)" % ", ".join("%s, %s" % (self.labels.get(h["bill_id"], h["bill_id"]), h["status"]) for h in holds)) if holds else "",
            max(in_shopify - held, 0)))
        rows = []
        for bill_id, label in self.labels.items():
            b = self.app.repo.get_bill(bill_id)
            if b is None or sku not in {l.sku for l in b.lines}:
                continue
            shop = orders_with_invoice(self.url, b.invoice_no) if b.invoice_no else []
            rows.append("%s: %s%s · %s" % (label, b.status.upper(), (" " + b.invoice_no) if b.invoice_no else "",
                                           ("Shopify %s %s" % (shop[0]["name"], shop[0]["financial_status"])) if shop else "not in Shopify orders"))
        for r in rows:
            print("      │ " + r)
        if not rows:
            print("      │ no bills for this frame yet")
        today_orders = [o for o in shopify_get(self.url, "orders.json?status=any&limit=250&location_id=%d" % BR)["orders"]
                        if o["created_at"].startswith(DAY)]
        print("      └ Shopify orders at Bel Road today: %s" % (", ".join("%s (%s)" % (o["name"], o["financial_status"]) for o in today_orders) or "none"))


def till_app(url: str, db: str) -> Tuple[PosApp, FaultInjector]:
    faults = FaultInjector()
    return PosApp(Settings(base_url=url, db_path=db, business_date=DAY), transport=faults), faults


def run(argv=()) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    logging.basicConfig(level=logging.WARNING, stream=sys.stdout, format="    log: %(message)s")
    logging.getLogger("eluno_pos").setLevel(logging.ERROR)  # retries are expected here; keep the timeline readable
    folder = tempfile.mkdtemp(prefix="eluno_pos_race_")
    print("\n" + "#" * 100)
    print("#  TWO TILLS, ONE LAST FRAME (Bel Road): what each till sees, second by second")
    print("#  Two POS processes share one store database and one Shopify. Fresh mock and database.")
    print("#" * 100)
    with MockServer(flaky=True, today=DAY) as mock:
        till1, f1 = till_app(mock.url, os.path.join(folder, "store.db"))
        till2, f2 = till_app(mock.url, os.path.join(folder, "store.db"))
        try:
            till1.billing.cutover(["BR"])
            till1.tills.open("BR", "S112", 300000)
            simulate(till1, f1, till2, mock.url)
        finally:
            till1.close()
            till2.close()
    print("\nTwo-tills scenario complete.")
    return 0


def pay_in_background(app: PosApp, bill_id: str, method: str, staff: str, clock: Clock0, label: str, result: dict) -> threading.Thread:
    def work():
        res, err = attempt(lambda: app.billing.pay(bill_id, method, staff))
        result["bill"], result["error"] = res, err
        b = app.repo.get_bill(bill_id)
        clock.say(label, "payment finished: %s" % (("%s %s" % (res.status.upper(), res.invoice_no)) if res else "%s (bill is %s)" % (err[:150], b.status.upper())))
    t = threading.Thread(target=work)
    t.start()
    return t


def simulate(till1: PosApp, f1: FaultInjector, till2: PosApp, url: str) -> None:
    clock = Clock0()
    labels: Dict[str, str] = {}
    board = Board(url, till2, labels)

    def sell_attempt(app: PosApp, who: str, staff: str, phone: str, sku: str, name: str) -> Optional[str]:
        """Till creates a bill for one frame (frame only, no lens) and pays by UPI if it is allowed to."""
        bill, err = attempt(lambda: app.billing.create(BillRequest("BR", staff, phone, "walk_in", (PairRequest(sku),)))[0])
        if bill is None:
            clock.say(who, "tries to bill %s -> %s" % (sku, err[:170]))
            return None
        labels[bill.bill_id] = "%s (%s)" % (who, name)
        paid, err = attempt(lambda: app.billing.pay(bill.bill_id, "upi", staff))
        clock.say(who, "bills %s -> %s" % (sku, ("%s %s, Shopify %s" % (paid.status.upper(), paid.invoice_no, paid.shopify_order_name)) if paid else err[:170]))
        return bill.bill_id

    # ------------------------------------------------------------------ A
    print("\n" + "=" * 100)
    print("A. Till 1's payment is slow (customer approving UPI on the phone, 3 s); Till 2 tries 1 s later")
    print("-" * 100)
    sku, item = "FR-0011", 92000011
    board.show(sku, item, "before anyone starts")
    clock.reset()
    a, _ = till1.billing.create(BillRequest("BR", "S109", "+919000000016", "walk_in", (PairRequest(sku),)))
    labels[a.bill_id] = "Till 1 (Sunita)"
    clock.say("Till 1", "Sunita's bill for %s created (Rs %s); starts UPI payment" % (sku, "%.2f" % (a.total / 100)))
    f1.delay("POST", "/terminal/charges", 3.0)
    result: dict = {}
    t = pay_in_background(till1, a.bill_id, "upi", "S109", clock, "Till 1", result)
    time.sleep(1.0)
    board.show(sku, item, "t+1s: Till 1 is in the middle of payment")
    sell_attempt(till2, "Till 2", "S110", "+919000000023", sku, "Gaurav")
    t.join()
    board.show(sku, item, "Till 1's payment went through")
    sell_attempt(till2, "Till 2", "S110", "+919000000023", sku, "Gaurav")

    # ------------------------------------------------------------------ B
    print("\n" + "=" * 100)
    print("B. Sunita returns the frame (changed her mind): it goes back on the shelf, and Till 2 can sell it")
    print("-" * 100)
    clock.reset()
    paid_a = till1.repo.get_bill(a.bill_id)
    r = till1.refunds.refund(paid_a.invoice_no, "BR", "S109", "CHANGED_MIND")
    clock.say("Till 1", "refund %s, credit note %s, frame restocked at Bel Road" % (r.status.upper(), r.credit_note_no))
    board.show(sku, item, "after the return")
    sell_attempt(till2, "Till 2", "S110", "+919000000023", sku, "Gaurav")
    board.show(sku, item, "Gaurav bought the returned frame")

    # ------------------------------------------------------------------ C
    print("\n" + "=" * 100)
    print("C. Till 1's card is declined after 2 s; Till 2 tries at 1 s and again after the decline")
    print("-" * 100)
    sku, item = "FR-0014", 92000014
    board.show(sku, item, "before anyone starts")
    clock.reset()
    c, _ = till1.billing.create(BillRequest("BR", "S109", "+919000000002", "walk_in", (PairRequest(sku),)))
    labels[c.bill_id] = "Till 1 (Anjali)"
    clock.say("Till 1", "Anjali's bill for %s created; she taps her card" % sku)
    f1.reject("POST", "/terminal/charges", status=402, body={"error": "card declined by issuer"}, after=2.0)
    result = {}
    t = pay_in_background(till1, c.bill_id, "card", "S109", clock, "Till 1", result)
    time.sleep(1.0)
    sell_attempt(till2, "Till 2", "S110", "+919000000017", sku, "Deepak")
    t.join()
    board.show(sku, item, "the card was declined: nothing charged, but the frame is still reserved for Anjali's bill")
    sell_attempt(till2, "Till 2", "S110", "+919000000017", sku, "Deepak")
    till1.billing.cancel(c.bill_id, "S109", "card declined; customer left")
    clock.say("Till 1", "Anjali leaves; her bill is CANCELLED (no invoice number was ever used)")
    board.show(sku, item, "after the cancel")
    sell_attempt(till2, "Till 2", "S110", "+919000000017", sku, "Deepak")
    board.show(sku, item, "Deepak bought it")

    # ------------------------------------------------------------------ D
    print("\n" + "=" * 100)
    print("D. Till 1's terminal stops answering; Till 2 tries at 1 s; the bill can only be cancelled by a manager")
    print("-" * 100)
    sku, item = "FR-0016", 92000016
    board.show(sku, item, "before anyone starts")
    clock.reset()
    d, _ = till1.billing.create(BillRequest("BR", "S109", "+919000000027", "walk_in", (PairRequest(sku),)))
    labels[d.bill_id] = "Till 1 (Vivek)"
    clock.say("Till 1", "Vivek's bill for %s created; UPI payment starts" % sku)
    f1.outage("POST", "/terminal/charges", times=1000)
    result = {}
    t = pay_in_background(till1, d.bill_id, "upi", "S109", clock, "Till 1", result)
    time.sleep(1.0)
    sell_attempt(till2, "Till 2", "S110", "+919000000024", sku, "Pallavi")
    t.join()
    f1.clear()
    board.show(sku, item, "terminal unreachable: the bill waits (money may or may not have moved), the frame stays reserved")
    sell_attempt(till2, "Till 2", "S110", "+919000000024", sku, "Pallavi")
    _, err = attempt(lambda: till1.billing.cancel(d.bill_id, "S109", "terminal down"))
    clock.say("Till 1", "salesperson tries to cancel -> %s" % err[:150])
    till1.billing.cancel(d.bill_id, "S109", "terminal down; terminal shows no payment", approver_id="S112")
    clock.say("Till 1", "manager S112 checks the terminal shows no payment and cancels the bill")
    board.show(sku, item, "after the manager's cancel")
    sell_attempt(till2, "Till 2", "S110", "+919000000024", sku, "Pallavi")
    board.show(sku, item, "Pallavi bought it")

    # ------------------------------------------------------------------ wrap-up
    print("\n" + "=" * 100)
    print("Bel Road at the end: every bill, and what reached Shopify")
    print("-" * 100)
    for bill_id, label in labels.items():
        b = till2.repo.get_bill(bill_id)
        shop = orders_with_invoice(url, b.invoice_no) if b.invoice_no else []
        print("  %-18s %-10s %-20s %s" % (label, b.status.upper(), b.invoice_no or "(no invoice no.)",
                                         ("Shopify %s %s" % (shop[0]["name"], shop[0]["financial_status"])) if shop else "never in Shopify"))
    left = till2.repo.hold_list(BR)
    print("  Frames still reserved at Bel Road: %s" % (", ".join(h["bill_id"] for h in left) or "none"))
    rep = till2.reports.day_end("BR", till2.clock.today())
    print("  Day-end Bel Road: %d bills in Shopify, net after refunds Rs %.2f, findings: %s" % (
        rep.bills, rep.net_after_refunds / 100, "; ".join(f.message for f in rep.findings) or "none"))
    print("  Rule shown: a frame is sellable only if Shopify has it AND no other bill is paying for it. A declined or unfinished")
    print("  payment keeps it reserved and never reaches Shopify's orders; the frame frees up only when that bill is paid or cancelled.")
