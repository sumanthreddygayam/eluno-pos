"""The Shopify boundary: domain <-> Admin API shapes, and the invoice rendered from a Shopify order."""
import os
import re
import unittest

from eluno_pos.adapters.shopify import bill_from_order, order_payload, refund_payload, sale_record_from_order
from eluno_pos.config import ROOT
from eluno_pos.domain.money import from_decimal_str as P
from eluno_pos.domain.refund import Refund, plan_refund
from eluno_pos.invoice_render import render_html, render_text

from .support import catalog, order, orders, store

MONEY = re.compile(r"^\d+\.\d{2}$")


def seed_bill(name, code):
    return bill_from_order(order(name), store(code), catalog())


class InvoiceRender(unittest.TestCase):
    def test_reproduces_the_sample_invoice_byte_for_byte(self):
        with open(os.path.join(ROOT, "starter-kit", "sample_invoice.txt"), encoding="utf-8", newline="") as f:
            expected = f.read()
        self.assertEqual(render_text(seed_bill("#1001", "HSR"), store("HSR")), expected)

    def test_html_has_the_numbers(self):
        html = render_html(seed_bill("#1001", "HSR"), store("HSR"))
        for s in ("ELU/HSR/FY26/00001", "19,072.15", "1,031.07", "Nineteen thousand seventy-two rupees and fifteen paise"):
            self.assertIn(s, html)


class ReadBack(unittest.TestCase):
    def test_every_seed_order_round_trips_its_totals(self):
        for o in orders():
            code = next(a["value"] for a in o["note_attributes"] if a["name"] == "store_code")
            b = bill_from_order(o, store(code), catalog())
            t = b.totals
            self.assertEqual((t.mrp, t.discount, t.tax, t.net), (P(o["subtotal_price"]), P(o["total_discounts"]), P(o["total_tax"]), P(o["total_price"])), o["name"])

    def test_sale_record(self):
        r = sale_record_from_order(order("#1007"))
        self.assertEqual((r.invoice_no, r.net, r.captured, r.payment_method), ("ELU/HSR/FY26/00002", 1849700, 1849700, "upi"))
        self.assertEqual([(x.amount, x.credit_note_no, x.reason_code) for x in r.refunds], [(1849700, "ELU/HSR/FY26/CN/00002", "CHANGED_MIND")])
        self.assertIsNone(sale_record_from_order({"id": 1, "note_attributes": [], "line_items": []}))


class Payloads(unittest.TestCase):
    def test_order_payload_shape(self):
        bill = seed_bill("#1001", "HSR")
        o = order_payload(bill)["order"]
        self.assertIs(o["taxes_included"], True)
        self.assertEqual((o["location_id"], o["inventory_behaviour"], o["source_name"]), (70000002, "decrement_ignoring_policy", "eluno-pos"))
        frame, lens = o["line_items"]
        for li in o["line_items"]:
            self.assertTrue(MONEY.match(li["price"]) and MONEY.match(li["total_discount"]))
            self.assertTrue(all(MONEY.match(t["price"]) for t in li["tax_lines"]))
            self.assertEqual(li["quantity"], 1)
        self.assertEqual([t["rate"] for t in frame["tax_lines"]], [0.09, 0.09])
        self.assertEqual([(t["title"], t["rate"], t["price"]) for t in lens["tax_lines"]], [("CGST", 0.025, "192.24"), ("SGST", 0.025, "192.24")])
        props = {p["name"]: p["value"] for p in lens["properties"]}
        self.assertEqual((props["rx_id"], props["lens_config"], props["line_type"], props["_line_ref"]), ("RX5007", "progressive/1.6/blue_cut", "lens", "1L"))
        self.assertEqual(o["discount_applications"][lens["discount_allocations"][0]["discount_application_index"]]["title"], "PGX_CAMPAIGN15")
        self.assertEqual(lens["discount_allocations"][0]["amount"], "1424.85")
        attrs = {a["name"]: a["value"] for a in o["note_attributes"]}
        self.assertEqual((attrs["invoice_no"], attrs["channel"], attrs["payment_method"]), ("ELU/HSR/FY26/00001", "whatsapp_booked", "upi"))
        self.assertEqual([(t["kind"], t["amount"], t["gateway"]) for t in o["transactions"]], [("sale", "19072.15", "razorpay_terminal")])

    def test_zero_value_remake_has_no_transaction_and_no_stock_move(self):
        o = order_payload(seed_bill("#1009", "IN"))["order"]
        self.assertNotIn("transactions", o)
        self.assertEqual(o["inventory_behaviour"], "bypass")
        self.assertIn({"name": "parent_invoice_no", "value": "ELU/IN/FY26/00002"}, o["note_attributes"])

    def test_refund_payload(self):
        bill = seed_bill("#1001", "HSR")
        refund = Refund("rfd_1", bill.bill_id, bill.invoice_no, "HSR", 70000002, plan_refund(bill, None, set()), "CHANGED_MIND", "S105",
                        "upi", "2026-10-22T12:00:00+05:30", credit_note_no="ELU/HSR/FY26/CN/00009", terminal_refund_id="trf_1")
        r = refund_payload(bill, refund)["refund"]
        self.assertEqual([(x["restock_type"], x.get("location_id")) for x in r["refund_line_items"]], [("return", 70000002), ("no_restock", None)])
        self.assertEqual(r["transactions"], [{"kind": "refund", "gateway": "razorpay_terminal", "amount": "19072.15", "parent_id": bill.shopify_sale_txn_id,
                                              "authorization": "trf_1"}])
        self.assertIn({"name": "credit_note_no", "value": "ELU/HSR/FY26/CN/00009"}, r["note_attributes"])


if __name__ == "__main__":
    unittest.main()
