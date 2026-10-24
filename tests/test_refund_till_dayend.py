"""Refund math, cash till arithmetic and the day-end report, against the seed fixtures."""
import unittest

from eluno_pos.adapters.shopify import bill_from_order, sale_record_from_order
from eluno_pos.domain.bill import PAID
from eluno_pos.domain.dayend import build_day_end
from eluno_pos.domain.errors import InvalidRequest, StateConflict
from eluno_pos.domain.money import from_decimal_str as P
from eluno_pos.domain.pricing import price_line
from eluno_pos.domain.refund import plan_refund
from eluno_pos.domain.till import TillSession

from .support import D, catalog, load, order, orders, store, taxes


def seed_bill(name, code):
    return bill_from_order(order(name), store(code), catalog())


class RefundMath(unittest.TestCase):
    def test_full_refund_matches_the_seed_refund_exactly(self):
        o = order("#1007")
        plan = plan_refund(seed_bill("#1007", "HSR"), None, set())
        expected = [(P(r["subtotal"]), P(r["total_tax"]), r["restock_type"], r["location_id"]) for r in o["refunds"][0]["refund_line_items"]]
        got = [(l.taxable, l.tax, "return" if l.restock else "no_restock", l.location_id) for l in plan]
        self.assertEqual(got, expected)
        self.assertEqual(sum(l.gross for l in plan), P(o["refunds"][0]["transactions"][0]["amount"]))

    def test_frame_restocks_at_the_billing_store_lens_never(self):
        plan = plan_refund(seed_bill("#1001", "HSR"), None, set())
        self.assertEqual([(l.kind, l.restock, l.location_id) for l in plan], [("frame", True, 70000002), ("lens", False, None)])

    def test_partial_then_rest(self):
        bill = seed_bill("#1003", "KMG")
        first = plan_refund(bill, ["2F"], set())
        self.assertEqual([l.line_ref for l in first], ["2F"])
        rest = plan_refund(bill, None, {"2F"})
        self.assertEqual([l.line_ref for l in rest], ["1F", "1L", "2L"])
        self.assertEqual(sum(l.gross for l in first + rest), bill.total)
        with self.assertRaises(StateConflict):
            plan_refund(bill, ["2F"], {"2F"})
        with self.assertRaises(StateConflict):
            plan_refund(bill, None, {"1F", "1L", "2F", "2L"})
        with self.assertRaises(InvalidRequest):
            plan_refund(bill, ["9F"], set())

    def test_refund_reverses_the_tax_charged_not_todays_rate(self):
        bill = seed_bill("#1006", "BR")
        old = price_line("1L", 199900, "90015000", [], taxes(), D("2025-09-01"))  # 12% before the rate change
        bill.lines[0].taxable, bill.lines[0].cgst, bill.lines[0].sgst = old.tax.taxable, old.tax.cgst, old.tax.sgst
        plan = plan_refund(bill, None, set())
        self.assertEqual(plan[0].tax, old.tax.tax)
        self.assertEqual(plan[0].tax, 21418)

    def test_only_synced_bills_and_not_zero_value(self):
        b = seed_bill("#1001", "HSR")
        b.status = PAID
        with self.assertRaises(StateConflict):
            plan_refund(b, None, set())
        with self.assertRaises(InvalidRequest):
            plan_refund(seed_bill("#1009", "IN"), None, set())  # zero-charge remake


class CashTill(unittest.TestCase):
    def replay(self, sample):
        s = TillSession(sample["session_id"], sample["store_code"], sample["location_id"], sample["opened_at"], sample["opened_by"],
                        sample["opening_float_paise"])
        for m in sample["movements"]:
            s.record(m["type"], abs(m["amount_paise"]), m["ref"], m["at"], sample["opened_by"], m["reason"] or None)
        s.close(sample["counted_cash_paise"], sample["closed_at"], sample["closed_by"], alert_threshold=20000)
        return s

    def test_seed_sessions(self):
        jn, vc = (self.replay(t) for t in load("till_sessions_sample.json"))
        self.assertEqual((jn.expected_cash_paise, jn.variance_paise, jn.variance_flagged), (1829600, -25000, True))
        self.assertEqual((vc.expected_cash_paise, vc.variance_paise, vc.variance_flagged), (719700, 0, False))

    def test_rules(self):
        s = TillSession("T", "HSR", 70000002, "2026-10-22T10:00:00+05:30", "S108", 100000)
        with self.assertRaises(InvalidRequest):
            s.record("paid_out", 5000, "PO-1", "t", "S108")  # no reason
        with self.assertRaises(StateConflict):
            s.record("paid_out", 100001, "PO-1", "t", "S108", "COURIER")  # more than the drawer holds
        s.record("paid_out", 20000, "PO-1", "t", "S108", "COURIER")
        s.close(80000 - 20000, "t", "S108", 20000)
        self.assertEqual((s.variance_paise, s.variance_flagged), (-20000, False))  # threshold is "over Rs 200"
        with self.assertRaises(StateConflict):
            s.record("paid_in", 100, "PI-1", "t", "S108", "FLOAT_TOP_UP")


class DayEnd(unittest.TestCase):
    records = [sale_record_from_order(o) for o in orders()]

    def test_jayanagar_day_matches_the_till_sample(self):
        r = build_day_end("JN", D("2026-10-06"), self.records)
        self.assertEqual(r.bills, 3)
        self.assertEqual(r.net_sales, 899800 + 549800 + 649700)
        self.assertEqual(r.by_payment_method["cash"].net, 899800 + 549800)  # = cash_sale movements in TILL-JN-2026-10-06
        self.assertEqual(r.by_payment_method["upi"].net, 649700)
        jn = [o for o in orders() if o["location_id"] == 70000005 and o["created_at"].startswith("2026-10-06")]
        self.assertEqual(r.tax, sum(P(o["total_tax"]) for o in jn))
        self.assertEqual(r.taxable + r.tax, r.net_sales)
        self.assertEqual(r.findings, [])

    def test_refund_counts_on_the_day_it_was_made(self):
        sale_day = build_day_end("HSR", D("2026-10-09"), self.records)
        self.assertEqual((sale_day.net_sales, sale_day.refunds_amount), (1849700, 0))
        refund_day = build_day_end("HSR", D("2026-10-12"), self.records)
        self.assertEqual((refund_day.bills, refund_day.refunds_amount, refund_day.refunds_tax), (0, 1849700, 205902 + 23805))
        self.assertEqual(refund_day.credit_notes, ["ELU/HSR/FY26/CN/00002"])
        self.assertEqual(refund_day.by_payment_method["upi"].net, -1849700)

    def test_discounts_channel_and_zero_value(self):
        r = build_day_end("HSR", D("2026-10-02"), self.records)
        self.assertEqual((r.discounts, r.discounts_by_offer, list(r.by_channel)), (142485, {"PGX_CAMPAIGN15": 142485}, ["whatsapp_booked"]))
        remake = build_day_end("IN", D("2026-10-15"), self.records)
        self.assertEqual((remake.bills, remake.zero_value_bills, remake.net_sales, remake.average_bill), (1, 1, 0, 0))


if __name__ == "__main__":
    unittest.main()
