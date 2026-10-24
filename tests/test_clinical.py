"""Prescription selection, lens-vs-Rx validation, anomaly flags, cart checks."""
import unittest

from eluno_pos.domain.cart import Cart, Pair, stock_issues, structural_issues
from eluno_pos.domain.catalog import LensChoice
from eluno_pos.domain.issues import BLOCK, CONFIRM, INFO, blocking
from eluno_pos.domain.lens_rules import anomaly_flags, check_lens
from eluno_pos.domain.prescription import EyeRx, Prescription, RxSelection, select_prescription

from .support import D, catalog, customer, prescriptions, staff, store

TODAY = D("2026-10-22")


def lens(text):
    return catalog().lens(LensChoice.parse(text))


def rx(sph=-300, cyl=0, axis=None, add=None, rx_id="RXT", issued="2026-06-01", until="2027-06-01", source="in_store", notes=None):
    e = EyeRx(sph, cyl, axis, add)
    return Prescription(rx_id, 1, "+919000000099", D(issued), D(until), source, e, e, 62, None, notes)


def codes(issues):
    return [(i.code, i.severity) for i in issues]


class SelectPrescription(unittest.TestCase):
    def test_newest_valid_wins_and_future_dated_is_skipped(self):
        sel = select_prescription(prescriptions("+919000000015"), TODAY)  # Varun: RX5023 dated 2027-01-10
        self.assertEqual(sel.chosen.rx_id, "RX5021")
        self.assertEqual(sel.skipped[0].rx_id, "RX5023")
        self.assertIn("after today", sel.skipped[0].reason)

    def test_no_valid_prescription(self):
        sel = select_prescription(prescriptions("+919000000012"), TODAY)  # Kavya: future + expired only
        self.assertIsNone(sel.chosen)
        self.assertEqual([s.rx_id for s in sel.skipped], ["RX5018", "RX5019", "RX5017"])
        self.assertEqual(codes(check_lens(lens("single_vision/1.50/standard"), sel, TODAY)), [("RX_NOT_VALID", BLOCK)])

    def test_validity_boundaries(self):
        r = rx(issued="2025-10-22", until="2026-10-22")
        self.assertEqual(select_prescription([r], TODAY).chosen, r)
        self.assertIsNone(select_prescription([r], D("2026-10-23")).chosen)


class LensAgainstRx(unittest.TestCase):
    def test_minus_six_on_1_56_is_blocked_with_alternatives(self):
        """Dr. Iyer's example: 1.56 for a -6.00 customer comes back from the lab."""
        issues = check_lens(lens("single_vision/1.56/standard"), RxSelection(rx(sph=-600)), TODAY, catalog())
        self.assertEqual(codes(issues), [("LENS_OUT_OF_RX_RANGE", BLOCK)])
        self.assertIn("-6.00 is outside the 1.56 range -4.00 to +4.00", issues[0].message)
        self.assertIn("1.60, 1.67, 1.74", issues[0].hint)
        self.assertEqual(check_lens(lens("single_vision/1.60/standard"), RxSelection(rx(sph=-600)), TODAY, catalog()), [])

    def test_limits_are_inclusive(self):
        self.assertEqual(check_lens(lens("single_vision/1.50/standard"), RxSelection(rx(sph=-400, cyl=-200, axis=90)), TODAY), [])
        issues = check_lens(lens("single_vision/1.50/standard"), RxSelection(rx(sph=-400, cyl=-225, axis=90)), TODAY)
        self.assertIn("cylinder -2.25 exceeds", issues[0].message)

    def test_progressive_needs_an_add_in_range(self):
        self.assertEqual(codes(check_lens(lens("progressive/1.60/standard"), RxSelection(rx(add=None)), TODAY)), [("ADD_MISSING", BLOCK)])
        self.assertEqual(codes(check_lens(lens("bifocal/1.60/standard"), RxSelection(rx(add=325)), TODAY)), [("ADD_OUT_OF_RANGE", BLOCK)])
        self.assertEqual(check_lens(lens("progressive/1.60/standard"), RxSelection(rx(add=250)), TODAY), [])

    def test_single_vision_with_an_add_is_only_a_note(self):
        self.assertEqual(codes(check_lens(lens("single_vision/1.60/standard"), RxSelection(rx(add=200)), TODAY)), [("ADD_NOT_USED", INFO)])


class AnomalyFlags(unittest.TestCase):
    def test_external_axis_off_grid_needs_confirmation(self):
        sel = select_prescription(prescriptions("+919000000003"), TODAY)  # RX5999 axis 87/92
        flags = anomaly_flags(lens("single_vision/1.56/standard"), sel)
        self.assertIn(("AXIS_OFF_GRID", CONFIRM), codes(flags))
        self.assertEqual(blocking(flags), [f for f in flags if f.code == "AXIS_OFF_GRID"])
        self.assertEqual(blocking(flags, acknowledged=["AXIS_OFF_GRID"]), [])

    def test_sphere_jump_since_last_test(self):
        new, old = rx(sph=-450, rx_id="NEW", issued="2026-09-01"), rx(sph=-300, rx_id="OLD", issued="2025-09-01", until="2026-09-01")
        sel = select_prescription([new, old], TODAY)
        self.assertIn(("SPH_JUMP", CONFIRM), codes(anomaly_flags(None, sel)))
        sel = select_prescription([rx(sph=-375, rx_id="NEW", issued="2026-09-01"), old], TODAY)
        self.assertNotIn("SPH_JUMP", [c for c, _ in codes(anomaly_flags(None, sel))])

    def test_first_progressive_mentions_the_badge(self):
        flags = anomaly_flags(lens("progressive/1.60/standard"), RxSelection(rx(add=150), (rx(add=150),)), staff("S106"))
        f = next(f for f in flags if f.code == "FIRST_PROGRESSIVE")
        self.assertIn("progressive_expert", f.hint)


class CartChecks(unittest.TestCase):
    def cart(self, *pairs, at="HSR"):
        c = customer("+919000000005")
        return Cart(store(at), c, staff("S105"), "walk_in", TODAY,
                    tuple(Pair(i, c, RxSelection(rx(add=200)), f, l) for i, (f, l) in enumerate(pairs, start=1)))

    def test_sunglasses_that_cannot_take_a_lens(self):
        c = self.cart((catalog().frame("FR-0004"), lens("single_vision/1.50/standard")))
        self.assertEqual(codes(structural_issues(c)), [("FRAME_NOT_RX_ABLE", BLOCK)])
        self.assertEqual(structural_issues(self.cart((catalog().frame("FR-0004"), None))), [])  # sunglasses alone are fine

    def test_archived_frame(self):
        self.assertEqual(codes(structural_issues(self.cart((catalog().frame("FR-0040"), None)))), [("FRAME_DISCONTINUED", BLOCK)])

    def test_out_of_stock_names_where_it_is(self):
        f = catalog().frame("FR-0009")
        stores = {s.location_id: s for s in (store("HSR"), store("MM"), store("BR"))}
        levels = {(f.inventory_item_id, 70000002): 0, (f.inventory_item_id, 70000001): 5, (f.inventory_item_id, 70000003): 2}
        issues = stock_issues(self.cart((f, None)), levels, stores)
        self.assertEqual(codes(issues), [("OUT_OF_STOCK", BLOCK)])
        self.assertIn("MM (5), BR (2)", issues[0].hint)
        levels[(f.inventory_item_id, 70000002)] = 1
        self.assertEqual(codes(stock_issues(self.cart((f, None), (f, None)), levels, stores)), [("OUT_OF_STOCK", BLOCK)])  # two of the same, one in stock

    def test_last_unit_held_by_another_till(self):
        f = catalog().frame("FR-0009")
        levels, stores = {(f.inventory_item_id, 70000002): 1}, {70000002: store("HSR")}
        self.assertEqual(stock_issues(self.cart((f, None)), levels, stores), [])
        issues = stock_issues(self.cart((f, None)), levels, stores, held={(f.inventory_item_id, 70000002): 1})
        self.assertEqual(codes(issues), [("STOCK_HELD", BLOCK)])
        self.assertIn("reserved by a bill on another till", issues[0].message)


if __name__ == "__main__":
    unittest.main()
