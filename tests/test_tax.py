"""GST back-out per line, checked against every line of the 12 seed orders (reconciled to the paisa)."""
import unittest
from datetime import date

from eluno_pos.domain.errors import ConfigError, DomainError
from eluno_pos.domain.money import from_decimal_str as P
from eluno_pos.domain.tax import TaxRate, TaxTable, back_out_gst

from .support import D, orders, taxes


class BackOut(unittest.TestCase):
    def test_every_seed_line_matches_to_the_paisa(self):
        table = taxes()
        checked = 0
        for o in orders():
            day = D(o["created_at"][:10])
            for li in o["line_items"]:
                hsn = next(p["value"] for p in li["properties"] if p["name"] == "hsn")
                gross = P(li["price"]) * li["quantity"] - P(li["total_discount"])
                t = back_out_gst(gross, table.rate_for(hsn, day).rate_bps)
                cgst, sgst = (P(x["price"]) for x in li["tax_lines"])
                self.assertEqual((t.cgst, t.sgst), (cgst, sgst), "%s %s" % (o["name"], li["sku"]))
                self.assertEqual(t.taxable + t.cgst + t.sgst, gross)
                checked += 1
        self.assertEqual(checked, 22)

    def test_sample_invoice_lines(self):
        frame = back_out_gst(1099800, 1800)
        self.assertEqual((frame.taxable, frame.cgst, frame.sgst), (932034, 83883, 83883))
        lens = back_out_gst(949900 - 142485, 500)
        self.assertEqual((lens.taxable, lens.cgst, lens.sgst), (768967, 19224, 19224))

    def test_odd_tax_paisa_goes_to_cgst(self):
        t = back_out_gst(699800, 1800)  # tax 106749 -> CGST 53375, SGST 53374 (seed #1003)
        self.assertEqual((t.cgst, t.sgst), (53375, 53374))

    def test_zero_and_negative(self):
        self.assertEqual(back_out_gst(0, 500).tax, 0)
        with self.assertRaises(DomainError):
            back_out_gst(-1, 500)


class RatesAreDataWithDates(unittest.TestCase):
    def test_lens_rate_changed_on_2025_09_22(self):
        t = taxes()
        self.assertEqual(t.rate_for("90015000", date(2025, 9, 21)).rate_bps, 1200)
        self.assertEqual(t.rate_for("90015000", date(2025, 9, 22)).rate_bps, 500)
        self.assertEqual(t.rate_for("90031900", date(2026, 10, 22)).rate_bps, 1800)
        self.assertEqual(t.rate_for("90049000", date(2026, 10, 22)).half_rate, 0.09)

    def test_unknown_hsn_blocks(self):
        with self.assertRaises(DomainError) as e:
            taxes().rate_for("99999999", date(2026, 10, 22))
        self.assertEqual(e.exception.code, "tax_rate_missing")

    def test_overlapping_rates_rejected(self):
        with self.assertRaises(ConfigError):
            TaxTable([TaxRate("1", 500, date(2025, 1, 1), None), TaxRate("1", 1200, date(2025, 6, 1), None)])

    def test_new_rate_is_a_data_change(self):
        data = {"rates": [{"hsn": "90015000", "rate_bps": 500, "effective_from": "2025-09-22", "effective_to": "2026-12-31"},
                          {"hsn": "90015000", "rate_bps": 1200, "effective_from": "2027-01-01"}]}
        t = TaxTable.from_json(data)
        self.assertEqual(back_out_gst(100000, t.rate_for("90015000", date(2027, 1, 1)).rate_bps).taxable, 89286)


if __name__ == "__main__":
    unittest.main()
