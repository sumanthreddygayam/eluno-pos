"""Offer rules: dates, conditions, stacking, priority, caps, manual overrides, and that offers are data."""
import copy
import unittest

from eluno_pos.domain.errors import InvalidRequest, PermissionDenied
from eluno_pos.domain.offers import ManualDiscount, OfferContext

from .support import D, engine, load, offer_book, offer_lines, staff, CONFIG

KARTHIK = frozenset({"campaign:progressive_oct26", "priority"})


def by_offer(ev):
    out = {}
    for a in ev.allocations:
        out[a.offer_id] = out.get(a.offer_id, 0) + a.amount
    return out


def decision(ev, oid):
    return next(d for d in ev.decisions if d.offer_id == oid)


class Stacking(unittest.TestCase):
    def test_seed_1001_better_combination_exists(self):
        """Seed order #1001 applied PGX_CAMPAIGN15 alone (Rs 1,424.85); the stackable set saves Rs 1,800."""
        ev = engine().evaluate(offer_lines("FR-0003", "progressive/1.60/blue_cut"), OfferContext(D("2026-10-02"), KARTHIK))
        self.assertEqual(by_offer(ev), {"FREE_BLUECUT_PGX": 100000, "PRIORITY500": 50000, "BUNDLE300": 30000})
        self.assertEqual(ev.total, 180000)
        pgx = decision(ev, "PGX_CAMPAIGN15")
        self.assertFalse(pgx.applied)
        self.assertIn("does not combine", pgx.why)
        self.assertIn("1,424.85", pgx.why)

    def test_priority_first_policy_is_a_setting(self):
        ev = engine("priority_first").evaluate(offer_lines("FR-0003", "progressive/1.60/blue_cut"), OfferContext(D("2026-10-02"), KARTHIK))
        self.assertEqual(by_offer(ev), {"FESTIVE10": 109980})

    def test_seed_1002_festive_on_frame(self):
        ev = engine().evaluate(offer_lines("FR-0015", "single_vision/1.50/anti_reflective"),
                               OfferContext(D("2026-10-03"), frozenset({"campaign:progressive_oct26", "family"})))
        self.assertEqual([(a.line_ref, a.offer_id, a.amount) for a in ev.allocations], [("1F", "FESTIVE10", 59980)])

    def test_seed_1003_two_pairs(self):
        lines = offer_lines("FR-0020", "single_vision/1.56/anti_reflective", "|", "FR-0031", "single_vision/1.50/standard")
        ev = engine().evaluate(lines, OfferContext(D("2026-10-05"), frozenset({"family"})))
        self.assertEqual(by_offer(ev), {"SECONDPAIR50": 49950, "BUNDLE300": 60000})
        self.assertIn(("2L", "SECONDPAIR50", 49950), [(a.line_ref, a.offer_id, a.amount) for a in ev.allocations])
        self.assertFalse(decision(ev, "FESTIVE10").applied)  # 1,049.60 alone < 1,099.50 stacked

    def test_flat_pair_discount_is_split_pro_rata_and_exact(self):
        ev = engine().evaluate(offer_lines("FR-0036", "single_vision/1.60/standard"), OfferContext(D("2026-01-15"), frozenset()))
        bundle = {a.line_ref: a.amount for a in ev.allocations if a.offer_id == "BUNDLE300"}
        self.assertEqual(sum(bundle.values()), 30000)
        self.assertEqual(bundle, {"1F": 16362, "1L": 13638})  # 30000 x 2998/5497 and the remainder

    def test_cap_is_shared_across_lines(self):
        ev = engine().evaluate(offer_lines("FR-0001", "|", "FR-0001"), OfferContext(D("2026-10-22"), frozenset()))
        self.assertEqual([(a.line_ref, a.amount) for a in ev.allocations], [("1F", 100000), ("2F", 100000)])
        self.assertIn("capped", decision(ev, "FESTIVE10").why)

    def test_second_pair_tie_goes_to_the_later_pair(self):
        lines = offer_lines("single_vision/1.50/standard", "|", "single_vision/1.50/standard")
        ev = engine().evaluate(lines, OfferContext(D("2026-10-22"), frozenset()))
        self.assertEqual([(a.line_ref, a.amount) for a in ev.allocations if a.offer_id == "SECONDPAIR50"], [("2L", 49950)])


class Eligibility(unittest.TestCase):
    def test_dates_are_inclusive(self):
        lines = offer_lines("FR-0015")
        self.assertTrue(decision(engine().evaluate(lines, OfferContext(D("2026-11-15"))), "FESTIVE10").applied)
        d = decision(engine().evaluate(lines, OfferContext(D("2026-11-16"))), "FESTIVE10")
        self.assertFalse(d.applied)
        self.assertIn("not valid on 2026-11-16", d.why)

    def test_inactive_and_untagged(self):
        ev = engine().evaluate(offer_lines("FR-0015", "progressive/1.50/standard"), OfferContext(D("2026-10-22")))
        self.assertEqual(decision(ev, "MONSOON20").why, "inactive")
        self.assertIn("not tagged campaign:progressive_oct26", decision(ev, "PGX_CAMPAIGN15").why)
        self.assertIn("needs 2 pairs", decision(ev, "SECONDPAIR50").why)

    def test_lens_conditions(self):
        ev = engine().evaluate(offer_lines("progressive/1.56/blue_cut"), OfferContext(D("2026-10-22")))
        self.assertIn("no qualifying lens", decision(ev, "FREE_BLUECUT_PGX").why)  # index 1.56 < 1.60
        ev = engine().evaluate(offer_lines("progressive/1.67/blue_cut"), OfferContext(D("2026-10-22")))
        self.assertEqual(by_offer(ev)["FREE_BLUECUT_PGX"], 100000)

    def test_discount_never_exceeds_the_line(self):
        raw = [{"offer_id": "BIG", "name": "big", "active": True, "valid_from": "2026-01-01", "valid_to": "2026-12-31", "stackable": True,
                "priority": 1, "rule": {"applies_to": "lens", "discount": {"type": "flat", "value_paise": 10000000}}}]
        ev = engine(raw=raw).evaluate(offer_lines("single_vision/1.50/standard"), OfferContext(D("2026-10-22")))
        self.assertEqual(ev.total, 99900)


class ManualOverride(unittest.TestCase):
    lines = offer_lines("FR-0003", "progressive/1.60/blue_cut")

    def ctx(self, approver="S108", bps=1000, reason="WALKOUT_SAVE", store="HSR"):
        return OfferContext(D("2026-10-22"), KARTHIK, store, ManualDiscount("MANAGER_OVERRIDE", bps, staff(approver), reason))

    def test_needs_a_store_manager_of_this_store(self):
        with self.assertRaises(PermissionDenied):
            engine().evaluate(self.lines, self.ctx(approver="S105"))
        with self.assertRaises(PermissionDenied):
            engine().evaluate(self.lines, self.ctx(store="KMG"))

    def test_needs_a_valid_reason_and_respects_the_cap(self):
        with self.assertRaises(InvalidRequest):
            engine().evaluate(self.lines, self.ctx(reason=""))
        with self.assertRaises(InvalidRequest):
            engine().evaluate(self.lines, self.ctx(reason="BECAUSE"))
        with self.assertRaises(InvalidRequest):
            engine().evaluate(self.lines, self.ctx(bps=1500))

    def test_competes_like_any_non_stackable_offer(self):
        ev = engine().evaluate(self.lines, self.ctx())
        self.assertEqual(by_offer(ev), {"MANAGER_OVERRIDE": 204970})
        ev = engine().evaluate(self.lines, self.ctx(bps=500))  # 5% = 1,024.85 < the Rs 1,800 stack
        self.assertFalse(decision(ev, "MANAGER_OVERRIDE").applied)
        self.assertEqual(ev.total, 180000)

    def test_never_applied_without_a_request(self):
        ev = engine().evaluate(self.lines, OfferContext(D("2026-10-22"), KARTHIK))
        self.assertIn("manual discount", decision(ev, "MANAGER_OVERRIDE").why)


class OffersAreData(unittest.TestCase):
    def test_making_festive_stackable_gives_meenas_combination(self):
        raw = copy.deepcopy(load("offers.json", CONFIG))
        next(o for o in raw if o["offer_id"] == "FESTIVE10")["stackable"] = True
        lines = offer_lines("FR-0003", "single_vision/1.50/standard", "|", "FR-0031", "single_vision/1.50/standard")
        ev = engine(raw=raw).evaluate(lines, OfferContext(D("2026-10-22"), frozenset({"priority"})))
        self.assertEqual(set(by_offer(ev)), {"FESTIVE10", "SECONDPAIR50", "PRIORITY500", "BUNDLE300"})
        self.assertEqual(by_offer(ev)["FESTIVE10"], 109980 + 34980)

    def test_malformed_offers_are_rejected_not_half_applied(self):
        good = load("offers.json", CONFIG)[0]
        bad = []
        for mutate in (lambda o: o.update(surprise=1),
                       lambda o: o["rule"].update(condition={"lens_type": "progressive"}),  # lens condition on a frame offer
                       lambda o: o["rule"].update(discount={"type": "percent", "value_bps_max": 1000}),  # open percent, no approver
                       lambda o: o.update(valid_from="2027-01-01"),
                       lambda o: o["rule"].update(discount={"type": "bogo"}),
                       lambda o: o["rule"]["discount"].update(on="lowest_priced_lens_line")):
            o = copy.deepcopy(good)
            mutate(o)
            o["offer_id"] = "BAD%d" % len(bad)
            bad.append(o)
        book = offer_book(bad + [good, copy.deepcopy(good)])
        self.assertEqual([o.offer_id for o in book.offers], ["FESTIVE10"])
        self.assertEqual(len(book.rejected), 7)
        self.assertIn("duplicate", book.rejected[-1][1])


if __name__ == "__main__":
    unittest.main()
