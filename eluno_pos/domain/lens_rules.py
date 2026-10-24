"""Clinical checks at billing time: is this lens makeable for this prescription?

The lab's limits come from the catalog (`rx_limits`), never from code. When a lens is blocked,
the message names the eye, the value, the limit, and which indices of the same type and coating
would fit, so sales staff can fix it on the spot instead of the lab sending it back.
"""
from __future__ import annotations

from datetime import date
from typing import List, Optional

from .catalog import Catalog, Lens
from .issues import BLOCK, CONFIRM, INFO, Issue
from .optics import fmt_d
from .parties import Staff
from .prescription import EyeRx, Prescription, RxSelection

SPH_JUMP_LIMIT = 100  # 1.00 D between consecutive prescriptions


def _eye_problems(lens: Lens, eye: str, e: EyeRx) -> List[str]:
    lim = lens.limits
    out = []
    if not (lim.sph_min <= e.sph <= lim.sph_max):
        out.append("%s sphere %s is outside the %s range %s to %s" % (eye, fmt_d(e.sph), lens.index, fmt_d(lim.sph_min), fmt_d(lim.sph_max)))
    if abs(e.cyl) > lim.cyl_abs_max:
        out.append("%s cylinder %s exceeds the %s limit of %s" % (eye, fmt_d(e.cyl), lens.index, fmt_d(lim.cyl_abs_max)))
    return out


def fits(lens: Lens, rx: Prescription) -> bool:
    if any(_eye_problems(lens, eye, e) for eye, e in rx.eyes()):
        return False
    if lens.limits.add_required:
        return all(e.add is not None and lens.limits.add_min <= e.add <= lens.limits.add_max for _, e in rx.eyes())
    return True


def check_lens(lens: Lens, selection: RxSelection, today: date, catalog: Optional[Catalog] = None,
               pair_ref: Optional[int] = None) -> List[Issue]:
    rx = selection.chosen
    if rx is None:
        latest = selection.latest_on_file
        why = "; ".join("%s %s" % (s.rx_id, s.reason) for s in selection.skipped) or "no prescription in the store tool"
        return [Issue("RX_NOT_VALID", BLOCK, "No valid prescription for this wearer (%s)." % why, pair_ref,
                      hint="Book an eye test, or have the optometrist record/renew the prescription in the store tool."
                      + (" Latest on file: %s." % latest.rx_id if latest else ""))]
    issues: List[Issue] = []
    problems = [p for eye, e in rx.eyes() for p in _eye_problems(lens, eye, e)]
    if problems:
        alternatives = [l.index for l in catalog.lens_family(lens.lens_type, lens.coating) if fits(l, rx)] if catalog else []
        hint = ("Indices that fit %s for this %s %s lens: %s." % (rx.rx_id, lens.lens_type.replace("_", " "), lens.coating.replace("_", " "), ", ".join(alternatives))
                if alternatives else "No %s index in the matrix fits %s; refer to the optometrist." % (lens.lens_type.replace("_", " "), rx.rx_id))
        issues.append(Issue("LENS_OUT_OF_RX_RANGE", BLOCK, "%s cannot be made for %s: %s." % (lens.label, rx.rx_id, "; ".join(problems)), pair_ref, hint))
    if lens.limits.add_required:
        missing = [eye for eye, e in rx.eyes() if e.add is None]
        if missing:
            issues.append(Issue("ADD_MISSING", BLOCK, "%s lenses need an ADD; %s has none for %s." % (lens.lens_type.replace("_", " ").title(), rx.rx_id, "/".join(missing)), pair_ref,
                                hint="Choose single vision, or ask the optometrist to record the near ADD."))
        else:
            bad = [(eye, e.add) for eye, e in rx.eyes() if not (lens.limits.add_min <= e.add <= lens.limits.add_max)]
            if bad:
                issues.append(Issue("ADD_OUT_OF_RANGE", BLOCK, "ADD %s outside the lab range %s to %s." % (
                    ", ".join("%s %s" % (eye, fmt_d(a)) for eye, a in bad), fmt_d(lens.limits.add_min), fmt_d(lens.limits.add_max)), pair_ref,
                    hint="Refer to the optometrist."))
    elif rx.has_add:
        issues.append(Issue("ADD_NOT_USED", INFO, "%s has an ADD but a single vision lens is selected." % rx.rx_id, pair_ref,
                            hint="Confirm the customer wants distance-only (or near-only) glasses."))
    return issues


def anomaly_flags(lens: Optional[Lens], selection: RxSelection, staff: Optional[Staff] = None,
                  pair_ref: Optional[int] = None) -> List[Issue]:
    """Billing-time anomaly flags (stretch): things that are legal but often turn into remakes."""
    rx = selection.chosen
    if rx is None:
        return []
    flags: List[Issue] = []
    off_grid = [(eye, e.axis) for eye, e in rx.eyes() if e.axis is not None and e.axis % 5 != 0]
    if off_grid:
        flags.append(Issue("AXIS_OFF_GRID", CONFIRM, "Axis %s on %s (%s) is not on the 5 degree grid." % (
            ", ".join("%s %d" % (eye, a) for eye, a in off_grid), rx.rx_id, rx.source), pair_ref,
            hint="Likely a transcription from paper; have the optometrist confirm before the lab order."))
    prev = selection.previous_to(rx)
    if prev:
        jumps = [(eye, e.sph - p.sph) for (eye, e), (_, p) in zip(rx.eyes(), prev.eyes()) if abs(e.sph - p.sph) > SPH_JUMP_LIMIT]
        if jumps:
            flags.append(Issue("SPH_JUMP", CONFIRM, "Sphere changed by more than 1.00 D since %s (%s)." % (
                prev.rx_id, ", ".join("%s %s" % (eye, fmt_d(d)) for eye, d in jumps)), pair_ref,
                hint="Ask the optometrist to confirm the new prescription."))
    if lens is not None and lens.lens_type == "progressive":
        first_time = not any(r.has_add for r in selection.history if r.rx_id != rx.rx_id) or "first-time progressive" in (rx.notes or "")
        if first_time:
            hint = "Brief the customer on adaptation."
            if staff is not None and not staff.is_progressive_expert:
                hint += " %s does not hold the progressive_expert badge; involve one who does." % staff.staff_id
            flags.append(Issue("FIRST_PROGRESSIVE", INFO, "First-time progressive wearer.", pair_ref, hint=hint))
    if rx.source == "external":
        flags.append(Issue("EXTERNAL_RX", INFO, "%s is an external prescription, not verified in store." % rx.rx_id, pair_ref))
    return flags
