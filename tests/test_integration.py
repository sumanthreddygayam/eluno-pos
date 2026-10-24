"""End-to-end against the starter-kit mock with 429/503 flakiness ON, plus injected network faults.

Each test gets a fresh mock (date pinned to 2026-10-22 so offers/prescriptions are stable) and a fresh POS database.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

from eluno_pos.api import Api, make_handler
from eluno_pos.app import PosApp
from eluno_pos.app.billing import BillRequest, PairRequest
from eluno_pos.config import Settings
from eluno_pos.devtools import FaultInjector, MockServer, free_port
from eluno_pos.domain.bill import DRAFT, PAID, SYNCED, VOIDED
from eluno_pos.domain.errors import BillBlocked, PaymentFailed, StateConflict
from eluno_pos.domain.money import to_decimal_str

DAY = "2026-10-22"
KARTHIK = BillRequest("HSR", "S105", "+919000000005", "whatsapp_booked", (PairRequest("FR-0003", "progressive/1.60/blue_cut"),))
SANJAY = BillRequest("BR", "S111", "+919000000021", "walk_in", (PairRequest("FR-0007", "progressive/1.60/blue_cut"),))
SURESH = BillRequest("JN", "S117", "+919000000003", "walk_in", (PairRequest("FR-0005", "single_vision/1.56/standard"),), acknowledge=("AXIS_OFF_GRID",))


class MockTestCase(unittest.TestCase):
    def setUp(self):
        self.mock = MockServer(flaky=True, today=DAY).__enter__()
        self.faults = FaultInjector()
        self.app = PosApp(Settings(base_url=self.mock.url, db_path=os.path.join(tempfile.mkdtemp(), "pos.db"), business_date=DAY),
                          transport=self.faults, sleep=lambda s: None)
        self.app.billing.cutover()  # go-live step, as in a real deployment

    def tearDown(self):
        self.app.close()
        self.mock.__exit__()

    def get(self, path):
        with urllib.request.urlopen(self.mock.url + path) as r:
            return json.loads(r.read())

    def shopify(self, path):
        return self.get("/admin/api/2026-10/" + path)

    def orders_with(self, invoice_no):
        return [o for o in self.shopify("orders.json?status=any&limit=250")["orders"]
                if {"name": "invoice_no", "value": invoice_no} in o["note_attributes"]]

    def stock(self, item, loc):
        return self.shopify("inventory_levels.json?inventory_item_ids=%d&location_ids=%d" % (item, loc))["inventory_levels"][0]["available"]

    def sell(self, req, method="upi"):
        bill, _ = self.app.billing.create(req)
        return self.app.billing.pay(bill.bill_id, method, req.staff_id)


class MustFlow(MockTestCase):
    def test_bill_charge_order_refund_day_end(self):
        before = self.stock(92000003, 70000002)
        bill = self.sell(KARTHIK)
        self.assertEqual((bill.status, bill.invoice_no), (SYNCED, "ELU/HSR/FY26/00003"))  # continues after Shopify POS's 00002

        [o] = self.orders_with(bill.invoice_no)
        self.assertEqual((o["total_price"], o["total_tax"], o["financial_status"]), (to_decimal_str(bill.total), to_decimal_str(bill.totals.tax), "paid"))
        for li, bl in zip(o["line_items"], bill.lines):
            self.assertEqual([t["price"] for t in li["tax_lines"]], [to_decimal_str(bl.cgst), to_decimal_str(bl.sgst)])
        self.assertIn({"name": "rx_id", "value": "RX5007"}, o["line_items"][1]["properties"])
        self.assertEqual(self.stock(92000003, 70000002), before - 1)
        charge = self.get("/terminal/charges/%s" % bill.charge_id)
        self.assertEqual((charge["amount_paise"], charge["order_ref"]), (bill.total, bill.bill_id))

        refund = self.app.refunds.refund(bill.invoice_no, "HSR", "S105", "CHANGED_MIND")
        self.assertEqual((refund.status, refund.credit_note_no, refund.amount), ("synced", "ELU/HSR/FY26/CN/00003", bill.total))
        [o] = self.orders_with(bill.invoice_no)
        self.assertEqual(o["financial_status"], "refunded")
        [rf] = o["refunds"]
        self.assertEqual(rf["transactions"][0]["parent_id"], o["transactions"][0]["id"])
        self.assertEqual([x["restock_type"] for x in rf["refund_line_items"]], ["return", "no_restock"])
        self.assertEqual(self.stock(92000003, 70000002), before)
        self.assertEqual(self.get("/terminal/charges/%s" % bill.charge_id)["refunded_paise"], bill.total)

        rep = self.app.reports.day_end("HSR", self.app.clock.today())
        self.assertEqual((rep.bills, rep.net_sales, rep.refunds_amount, rep.net_after_refunds), (1, bill.total, bill.total, 0))
        self.assertEqual([f for f in rep.findings if f.severity == "error"], [])

    def test_a_bill_after_yes_costs_three_shopify_requests(self):
        """Customer and prescription are remembered for the visit; stock is always asked fresh."""
        from eluno_pos.adapters.http import urllib_transport
        calls = []

        def counting(method, url, headers, body, timeout):
            status, h, raw = urllib_transport(method, url, headers, body, timeout)
            if "/admin/api/" in url and status < 300:  # the mock's random "busy" answers are retries, not extra work
                calls.append((method, url.split("?")[0].rsplit("/", 1)[-1]))
            return status, h, raw

        self.app.billing.lookup("+919000000005")                     # customer arrives: read fresh
        self.app.billing.quote(KARTHIK)                              # what was on screen
        self.faults.inner = counting
        bill, _ = self.app.billing.create(KARTHIK)                   # customer says yes
        self.app.billing.pay(bill.bill_id, "upi")
        self.assertEqual([c[1] for c in calls], ["inventory_levels.json", "inventory_levels.json", "orders.json"])
        # the order create is also what takes the frame off Shopify's stock: no separate inventory write
        self.assertEqual([c for c in calls if "adjust" in c[1] or "set.json" in c[1]], [])

    def test_quote_changed_since_the_customer_said_yes(self):
        bill, _ = self.app.billing.create(KARTHIK)
        req = urllib.request.Request(self.mock.url + "/admin/api/2026-10/inventory_levels/set.json", method="POST",
                                     data=json.dumps({"location_id": 70000002, "inventory_item_id": 92000003, "available": 0}).encode(),
                                     headers={"Content-Type": "application/json"})
        for _ in range(5):  # the mock 429s some writes
            try:
                urllib.request.urlopen(req).read()
                break
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise
        with self.assertRaises(BillBlocked) as e:
            self.app.billing.pay(bill.bill_id, "upi")
        self.assertEqual([i.code for i in e.exception.issues], ["OUT_OF_STOCK"])
        b = self.app.billing.get(bill.bill_id)
        self.assertEqual((b.status, b.charge_id, b.invoice_no), (DRAFT, None, None))  # nothing charged, no number used


class Idempotency(MockTestCase):
    def test_lost_response_does_not_create_a_second_order(self):
        self.faults.drop_response("POST", "/orders.json")
        bill = self.sell(SANJAY, "card")
        self.assertEqual(bill.status, SYNCED)
        self.assertEqual(len(self.orders_with(bill.invoice_no)), 1)
        synced = [e for e in self.app.repo.audit_log(ref=bill.bill_id) if e["action"] == "bill.synced"]
        self.assertTrue(synced[0]["details"]["adopted_existing"])

    def test_repeating_pay_is_harmless(self):
        bill = self.sell(SANJAY, "card")
        again = self.app.billing.pay(bill.bill_id, "card")
        self.assertEqual((again.status, again.charge_id, again.invoice_no), (SYNCED, bill.charge_id, bill.invoice_no))
        self.assertEqual(len(self.orders_with(bill.invoice_no)), 1)

    def test_crash_after_shopify_applied_the_write_recovers_by_adoption(self):
        """Order lands in Shopify, the response is lost AND the lookup fails: next sync must adopt, not duplicate."""
        self.faults.drop_response("POST", "/orders.json")
        self.faults.outage("GET", "/orders.json")
        bill = self.sell(SANJAY, "card")
        self.assertEqual(bill.status, PAID)
        self.assertEqual(len(self.orders_with(bill.invoice_no)), 1)  # it is there; we just could not see it
        self.faults.clear()
        self.app.sync_pending()
        bill = self.app.billing.get(bill.bill_id)
        self.assertEqual(bill.status, SYNCED)
        self.assertEqual(len(self.orders_with(bill.invoice_no)), 1)

    def test_outage_keeps_the_invoice_and_syncs_later(self):
        before = self.stock(92000003, 70000002)
        self.faults.outage("POST", "/orders.json")
        self.faults.outage("GET", "/orders.json")
        bill = self.sell(KARTHIK)
        self.assertEqual((bill.status, bill.invoice_no), (PAID, "ELU/HSR/FY26/00003"))
        # Shopify has not been told yet, so the POS holds the frame for the other tills
        self.assertEqual(self.stock(92000003, 70000002), before)
        self.assertEqual(self.app.repo.holds([(92000003, 70000002)]), {(92000003, 70000002): 1})
        self.faults.clear()
        rep = self.app.reports.day_end("HSR", self.app.clock.today())
        self.assertTrue(any("not in Shopify yet" in f.message for f in rep.findings))
        self.app.sync_pending()
        self.assertEqual(self.app.billing.get(bill.bill_id).status, SYNCED)
        self.assertEqual(len(self.orders_with(bill.invoice_no)), 1)
        # now Shopify's own count has the sale, so the hold is gone
        self.assertEqual(self.stock(92000003, 70000002), before - 1)
        self.assertEqual(self.app.repo.holds([(92000003, 70000002)]), {})

    def test_two_tills_cannot_sell_the_last_frame(self):
        """HSR has one FR-0005. Two customers want it; the first is mid-payment when the second pays."""
        self.assertEqual(self.stock(92000005, 70000002), 1)
        first, _ = self.app.billing.create(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0005"),)))
        second, _ = self.app.billing.create(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)))
        self.faults.outage("POST", "/terminal/charges")  # the first customer's card machine hangs
        with self.assertRaises(Exception):
            self.app.billing.pay(first.bill_id, "card")
        self.assertEqual(self.app.billing.get(first.bill_id).status, "payment_pending")
        with self.assertRaises(BillBlocked) as e:  # the second till is refused before charging anything
            self.app.billing.pay(second.bill_id, "upi")
        self.assertEqual([i.code for i in e.exception.issues], ["STOCK_HELD"])
        self.assertEqual((self.app.billing.get(second.bill_id).status, self.app.billing.get(second.bill_id).charge_id), (DRAFT, None))
        with self.assertRaises(BillBlocked) as e:  # a third customer can't even start a bill for it
            self.app.billing.create(BillRequest("HSR", "S105", "+919000000005", "walk_in", (PairRequest("FR-0005"),)))
        self.assertEqual([i.code for i in e.exception.issues], ["STOCK_HELD"])
        self.faults.clear()
        done = self.app.billing.pay(first.bill_id, "card")  # the card machine is back
        self.assertEqual(done.status, SYNCED)
        self.assertEqual((self.stock(92000005, 70000002), self.app.repo.holds([(92000005, 70000002)])), (0, {}))
        with self.assertRaises(BillBlocked) as e:  # now it is simply sold out
            self.app.billing.pay(second.bill_id, "upi")
        self.assertEqual([i.code for i in e.exception.issues], ["OUT_OF_STOCK"])

    def test_declined_payment_keeps_the_frame_until_the_bill_is_cancelled(self):
        key = (92000005, 70000002)  # HSR's only FR-0005
        bill, _ = self.app.billing.create(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0005"),)))
        self.faults.reject("POST", "/terminal/charges", status=402, body={"error": "card declined"})
        with self.assertRaises(PaymentFailed) as e:
            self.app.billing.pay(bill.bill_id, "card")
        self.assertEqual(e.exception.code, "payment_declined")
        b = self.app.billing.get(bill.bill_id)
        self.assertEqual((b.status, b.charge_id, b.invoice_no), (DRAFT, None, None))
        self.assertEqual(self.app.repo.holds([key]), {key: 1})  # still reserved: the customer may pay another way
        self.assertEqual(self.orders_with(b.invoice_no or "none"), [])  # never in Shopify's orders
        with self.assertRaises(BillBlocked) as e:
            self.app.billing.create(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)))
        self.assertEqual([i.code for i in e.exception.issues], ["STOCK_HELD"])
        rep = self.app.reports.day_end("HSR", self.app.clock.today())
        self.assertTrue(any("still reserved by unfinished bills" in f.message for f in rep.findings))
        self.app.billing.cancel(bill.bill_id, "S105", "customer left")
        self.assertEqual(self.app.repo.holds([key]), {})
        other = self.sell(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)), "upi")
        self.assertEqual(other.status, SYNCED)

    def test_stuck_terminal_payment_can_only_be_cancelled_by_a_manager(self):
        bill, _ = self.app.billing.create(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0005"),)))
        self.faults.outage("POST", "/terminal/charges")
        with self.assertRaises(PaymentFailed):
            self.app.billing.pay(bill.bill_id, "upi")
        self.faults.clear()
        self.assertEqual(self.app.billing.get(bill.bill_id).status, "payment_pending")
        with self.assertRaises(Exception) as e:
            self.app.billing.cancel(bill.bill_id, "S105", "terminal down")
        self.assertEqual(e.exception.code, "approver_required")
        with self.assertRaises(Exception):
            self.app.billing.cancel(bill.bill_id, "S105", "terminal down", approver_id="S106")  # sales staff can't approve
        cancelled = self.app.billing.cancel(bill.bill_id, "S105", "terminal shows no payment", approver_id="S108")
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(self.app.repo.holds([(92000005, 70000002)]), {})
        paid = self.sell(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0005"),)), "upi")
        with self.assertRaises(StateConflict):  # a bill with money taken can never be cancelled
            self.app.billing.cancel(paid.bill_id, "S105", "oops", approver_id="S108")

    def test_two_tills_in_parallel_one_second_apart(self):
        """Two POS processes on one store database: Till 1 is mid-payment (slow terminal) when Till 2 tries."""
        import threading
        import time as _t
        f1 = FaultInjector()
        till1 = PosApp(Settings(base_url=self.mock.url, db_path=self.app.settings.db_path, business_date=DAY), transport=f1, sleep=lambda s: None)
        try:
            bill, _ = till1.billing.create(BillRequest("HSR", "S105", "+919000000023", "walk_in", (PairRequest("FR-0005"),)))
            f1.delay("POST", "/terminal/charges", 2.0)
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("bill", till1.billing.pay(bill.bill_id, "upi")))
            t.start()
            _t.sleep(1.0)
            with self.assertRaises(BillBlocked) as e:  # Till 2 (self.app) while Till 1 waits on the terminal
                self.app.billing.create(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)))
            self.assertEqual([i.code for i in e.exception.issues], ["STOCK_HELD"])
            t.join()
            self.assertEqual(out["bill"].status, SYNCED)
            with self.assertRaises(BillBlocked) as e:
                self.app.billing.create(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)))
            self.assertEqual([i.code for i in e.exception.issues], ["OUT_OF_STOCK"])
            self.app.refunds.refund(out["bill"].invoice_no, "HSR", "S105", "CHANGED_MIND")  # returned: back on the shelf
            again = self.sell(BillRequest("HSR", "S106", "+919000000016", "walk_in", (PairRequest("FR-0005"),)), "upi")
            self.assertEqual(again.status, SYNCED)
        finally:
            till1.close()

    def test_void_returns_the_money_and_keeps_the_number_as_cancelled(self):
        self.faults.outage("POST", "/orders.json")
        self.faults.outage("GET", "/orders.json")
        bill = self.sell(KARTHIK)
        with self.assertRaises(Exception):
            self.app.billing.void(bill.bill_id, "S105", "S106", "SHOPIFY_REJECTED")  # needs a store manager
        self.assertEqual(self.app.repo.holds([(92000003, 70000002)]), {(92000003, 70000002): 1})
        bill = self.app.billing.void(bill.bill_id, "S105", "S108", "SHOPIFY_REJECTED")
        self.assertEqual(bill.status, VOIDED)
        self.assertEqual(self.app.repo.holds([(92000003, 70000002)]), {})  # sale undone, frame back on the shelf
        self.assertEqual(self.get("/terminal/charges/%s" % bill.charge_id)["refunded_paise"], bill.total)
        self.assertEqual([d["status"] for d in self.app.repo.documents("HSR", "invoice")], ["cancelled"])
        self.faults.clear()
        self.app.sync_pending()
        self.assertEqual(self.orders_with(bill.invoice_no), [])
        nxt = self.sell(KARTHIK)
        self.assertEqual(nxt.invoice_no, "ELU/HSR/FY26/00004")  # the cancelled number is not reused


    def test_no_series_and_shopify_down_refuses_before_charging(self):
        app = PosApp(Settings(base_url=self.mock.url, db_path=os.path.join(tempfile.mkdtemp(), "fresh.db"), business_date=DAY),
                     transport=self.faults, sleep=lambda s: None)
        try:
            bill, _ = app.billing.create(KARTHIK)
            self.faults.outage("GET", "/orders.json")
            with self.assertRaises(StateConflict) as e:
                app.billing.pay(bill.bill_id, "upi")
            self.assertEqual(e.exception.code, "series_not_ready")
            b = app.billing.get(bill.bill_id)
            self.assertEqual((b.status, b.charge_id), (DRAFT, None))
        finally:
            app.close()

    def test_refund_survives_a_lost_response_and_an_outage(self):
        bill = self.sell(KARTHIK)
        self.faults.drop_response("POST", "/refunds.json")
        r = self.app.refunds.refund(bill.invoice_no, "HSR", "S105", "CHANGED_MIND", ["1F"])
        self.assertEqual(r.status, "synced")
        self.faults.outage("POST", "/refunds.json")
        r2 = self.app.refunds.refund(bill.invoice_no, "HSR", "S105", "CHANGED_MIND", ["1L"])
        self.assertEqual((r2.status, r2.credit_note_no), ("money_returned", "ELU/HSR/FY26/CN/00004"))
        self.faults.clear()
        self.app.sync_pending()
        self.assertEqual(self.app.repo.get_refund(r2.refund_id).status, "synced")
        [o] = self.orders_with(bill.invoice_no)
        self.assertEqual(sorted(a["value"] for rf in o["refunds"] for a in rf["note_attributes"] if a["name"] == "credit_note_no"),
                         ["ELU/HSR/FY26/CN/00003", "ELU/HSR/FY26/CN/00004"])
        self.assertEqual(o["financial_status"], "refunded")


class StoreOperations(MockTestCase):
    def test_cash_needs_an_open_till_and_reconciles_at_day_end(self):
        bill, _ = self.app.billing.create(SURESH)
        with self.assertRaises(StateConflict):
            self.app.billing.pay(bill.bill_id, "cash")
        self.app.tills.open("JN", "S120", 300000)
        bill = self.app.billing.pay(bill.bill_id, "cash")
        self.app.tills.move("JN", "S120", "paid_out", 12000, "COURIER")
        till = self.app.tills.close("JN", "S120", 300000 + bill.total - 12000)
        self.assertEqual((till.variance_paise, till.variance_flagged), (0, False))
        rep = self.app.reports.day_end("JN", self.app.clock.today())
        self.assertEqual(rep.by_payment_method["cash"].net, bill.total)
        self.assertEqual(rep.till["expected"], 300000 + bill.total - 12000)
        self.assertEqual([f.message for f in rep.findings if f.severity != "info"], [])

    def test_lens_only_and_sunglasses_only_bills(self):
        lens_only = self.sell(BillRequest("BR", "S109", "+919000000018", "walk_in", (PairRequest(lens="single_vision/1.50/blue_cut"),)), "upi")
        [o] = self.orders_with(lens_only.invoice_no)
        self.assertEqual((len(o["line_items"]), o["line_items"][0]["sku"]), (1, "LN-0003"))
        self.assertIn({"name": "rx_id", "value": "RX5028"}, o["line_items"][0]["properties"])  # RX5029 is future-dated
        # Kavya has no valid prescription, which does not stop her buying sunglasses
        sun = self.sell(BillRequest("JN", "S117", "+919000000012", "walk_in", (PairRequest("FR-0004"),)), "upi")
        [o] = self.orders_with(sun.invoice_no)
        self.assertEqual([t["rate"] for t in o["line_items"][0]["tax_lines"]], [0.09, 0.09])  # HSN 90049000 at 18%

    def test_family_bill_carries_each_wearers_prescription(self):
        req = BillRequest("IN", "S121", "+919000000007", "walk_in", (PairRequest("FR-0001", "progressive/1.50/standard"),
                                                                     PairRequest("FR-0014", "single_vision/1.50/anti_reflective", "+919000000010")))
        bill = self.sell(req)
        [o] = self.orders_with(bill.invoice_no)
        props = [{p["name"]: p["value"] for p in li["properties"]} for li in o["line_items"]]
        self.assertEqual([p.get("rx_id") for p in props], [None, "RX5011", None, "RX5015"])
        self.assertEqual(props[3]["for_customer_id"], "80001010")
        self.assertEqual(o["customer"]["id"], 80001007)

    def test_pre_cutover_cash_bill_refund(self):
        """ELU/JN/FY26/00002 was billed by Shopify POS: read from Shopify, refunded in cash from the JN till."""
        self.app.tills.open("JN", "S120", 300000)
        with self.assertRaises(StateConflict):  # a Rs 5,498 cash refund cannot come out of a Rs 3,000 drawer
            self.app.refunds.refund("ELU/JN/FY26/00002", "JN", "S117", "CHANGED_MIND")
        self.app.tills.move("JN", "S120", "paid_in", 300000, "FLOAT_TOP_UP")
        before = self.stock(92000022, 70000005)
        r = self.app.refunds.refund("ELU/JN/FY26/00002", "JN", "S117", "CHANGED_MIND")
        self.assertEqual((r.status, r.credit_note_no, r.method, r.amount), ("synced", "ELU/JN/FY26/CN/00001", "cash", 549800))
        self.assertEqual(self.shopify("orders/95000011.json")["order"]["financial_status"], "refunded")
        self.assertEqual(self.stock(92000022, 70000005), before + 1)
        self.assertEqual(self.app.tills.current("JN").expected_cash_paise, 600000 - 549800)

    def test_remake_is_lens_only_zero_charge_and_leaves_frame_stock(self):
        parent = self.sell(SANJAY, "card")
        before = self.stock(92000007, 70000003)
        remake = self.app.remakes.remake(parent.invoice_no, "BR", "S111", "PROGRESSIVE_NON_ADAPTATION", "eluno")
        [o] = self.orders_with(remake.invoice_no)
        self.assertEqual((o["total_price"], o["transactions"], len(o["line_items"])), ("0.00", [], 1))
        self.assertIn({"name": "parent_invoice_no", "value": parent.invoice_no}, o["note_attributes"])
        self.assertEqual(self.stock(92000007, 70000003), before)


class BusinessDay(MockTestCase):
    def test_the_five_order_day_covers_every_requirement(self):
        """python run.py day, silently: every Must, Should, the stretch and the constraints are exercised."""
        import contextlib
        import io
        from eluno_pos import day
        day.EVIDENCE.clear()
        with contextlib.redirect_stdout(io.StringIO()):
            day.simulate(self.app, self.mock.url, self.faults, tempfile.mkdtemp())
        self.assertEqual([c for c, _ in day.REQUIREMENTS if c not in day.EVIDENCE], [])
        reports = {s: self.app.reports.day_end(s, self.app.clock.today()) for s in day.STORES}
        self.assertEqual(sum(r.bills for r in reports.values()), 5)  # 4 sales + 1 zero-value remake; the voided bill never reached Shopify
        self.assertEqual([f.message for r in reports.values() for f in r.findings if f.severity == "error"], [])


class HttpApi(MockTestCase):
    def setUp(self):
        super().setUp()
        self.port = free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), make_handler(Api(self.app)))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        super().tearDown()

    def call(self, method, path, body=None):
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method,
                                     data=json.dumps(body).encode() if body is not None else None, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read().decode()
                return r.status, json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_bill_invoice_refund_report(self):
        body = {"store_code": "HSR", "staff_id": "S105", "customer_phone": "+919000000005", "channel": "walk_in",
                "pairs": [{"frame_sku": "FR-0003", "lens": "progressive/1.60/blue_cut"}], "pay": "upi"}
        status, out = self.call("POST", "/bills", body)
        self.assertEqual((status, out["bill"]["status"]), (201, "synced"))
        inv = out["bill"]["invoice_no"]
        status, text = self.call("GET", "/invoices/%s.txt" % urllib.parse.quote(inv, safe=""))
        self.assertEqual(status, 200)
        self.assertIn(inv, text)
        status, rf = self.call("POST", "/refunds", {"invoice_no": inv, "store_code": "HSR", "staff_id": "S105", "reason_code": "CHANGED_MIND"})
        self.assertEqual((status, rf["status"]), (201, "synced"))
        status, rep = self.call("GET", "/reports/day-end?store=HSR&date=%s" % DAY)
        self.assertEqual((status, rep["net_after_refunds"]), (200, 0))

    def test_one_store_waiting_on_its_terminal_does_not_block_another_store(self):
        import time as _t
        bill, _ = self.app.billing.create(KARTHIK)
        self.faults.delay("POST", "/terminal/charges", 2.0)  # HSR's customer takes 2 s to approve
        slow = threading.Thread(target=lambda: self.call("POST", "/bills/%s/pay" % bill.bill_id, {"method": "upi"}))
        slow.start()
        _t.sleep(0.3)
        t0 = _t.monotonic()
        status, out = self.call("GET", "/customers/+919000000021")  # BR looks up a customer meanwhile
        took = _t.monotonic() - t0
        slow.join()
        self.assertEqual((status, out["customer"]["name"]), (200, "Sanjay Rao"))
        self.assertLess(took, 1.0)
        self.assertEqual(self.app.billing.get(bill.bill_id).status, SYNCED)

    def test_double_clicked_pay_charges_once(self):
        bill, _ = self.app.billing.create(KARTHIK)
        self.faults.delay("POST", "/terminal/charges", 1.0)
        results = []
        clicks = [threading.Thread(target=lambda: results.append(self.call("POST", "/bills/%s/pay" % bill.bill_id, {"method": "upi"})))
                  for _ in range(2)]
        for c in clicks:
            c.start()
        for c in clicks:
            c.join()
        self.assertEqual([r[0] for r in results], [200, 200])
        self.assertEqual({r[1]["charge_id"] for r in results}, {self.app.billing.get(bill.bill_id).charge_id})  # one charge
        self.assertEqual(len(self.orders_with(self.app.billing.get(bill.bill_id).invoice_no)), 1)

    def test_errors_are_actionable(self):
        body = {"store_code": "BR", "staff_id": "S111", "customer_phone": "+919000000021", "pairs": [{"frame_sku": "FR-0007", "lens": "progressive/1.56/blue_cut"}]}
        status, out = self.call("POST", "/bills", body)
        self.assertEqual((status, out["error"]["code"]), (422, "bill_blocked"))
        self.assertIn("1.60, 1.67, 1.74", out["error"]["details"]["issues"][0]["hint"])
        status, out = self.call("GET", "/customers/+919999999999")
        self.assertEqual((status, out["error"]["code"]), (404, "customer_not_found"))


if __name__ == "__main__":
    unittest.main()
