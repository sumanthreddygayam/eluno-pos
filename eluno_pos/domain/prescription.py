"""Prescriptions live in the store tool. The till never edits one: it decides whether one is usable, and it can
record a new one (an eye test here, or a paper prescription from another optometrist) after checking it."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import List, Optional, Tuple

from .optics import cd_to_float


@dataclass(frozen=True)
class EyeRx:
    sph: int
    cyl: int
    axis: Optional[int]
    add: Optional[int]


@dataclass(frozen=True)
class Prescription:
    rx_id: str
    customer_id: int
    phone: str
    issued_on: date
    valid_until: date
    source: str  # in_store | external
    od: EyeRx  # right eye
    os: EyeRx  # left eye
    pd: Optional[float] = None
    optometrist_id: Optional[str] = None
    notes: Optional[str] = None

    def eyes(self) -> Tuple[Tuple[str, EyeRx], Tuple[str, EyeRx]]:
        return (("OD", self.od), ("OS", self.os))

    @property
    def has_add(self) -> bool:
        return self.od.add is not None or self.os.add is not None

    def usable_on(self, day: date) -> bool:
        return self.issued_on <= day <= self.valid_until

    def line_property(self) -> str:
        """Compact JSON carried on the Shopify lens line, same shape as the orders already there."""
        def eye(e: EyeRx) -> dict:
            return {"sph": cd_to_float(e.sph), "cyl": cd_to_float(e.cyl), "axis": e.axis, "add": cd_to_float(e.add)}
        pd = int(self.pd) if self.pd is not None and float(self.pd).is_integer() else self.pd
        return json.dumps({"od": eye(self.od), "os": eye(self.os), "pd": pd}, separators=(",", ":"))


@dataclass(frozen=True)
class SkippedRx:
    rx_id: str
    reason: str


@dataclass(frozen=True)
class RxSelection:
    """Result of picking the prescription to bill against.

    `chosen` is the newest prescription usable today; `history` is every prescription newest first
    (used for change-since-last-test checks); `skipped` says why newer ones were passed over.
    """
    chosen: Optional[Prescription]
    history: Tuple[Prescription, ...] = ()
    skipped: Tuple[SkippedRx, ...] = ()

    def previous_to(self, rx: Prescription) -> Optional[Prescription]:
        older = [r for r in self.history if r.issued_on < rx.issued_on]
        return older[0] if older else None

    @property
    def latest_on_file(self) -> Optional[Prescription]:
        return self.history[0] if self.history else None


def select_prescription(rxs: List[Prescription], today: date) -> RxSelection:
    """Newest prescription with issued_on <= today <= valid_until.

    Future-dated prescriptions exist in the store tool data; they are skipped and reported,
    never used, because a test cannot have happened after today.
    """
    ordered = tuple(sorted(rxs, key=lambda r: (r.issued_on, r.rx_id), reverse=True))
    skipped: List[SkippedRx] = []
    for rx in ordered:
        if rx.issued_on > today:
            skipped.append(SkippedRx(rx.rx_id, "dated %s, after today; looks like a data-entry error in the store tool" % rx.issued_on))
        elif rx.valid_until < today:
            skipped.append(SkippedRx(rx.rx_id, "expired on %s" % rx.valid_until))
        else:
            return RxSelection(chosen=rx, history=tuple(r for r in ordered if r.issued_on <= today), skipped=tuple(skipped))
    return RxSelection(chosen=None, history=tuple(r for r in ordered if r.issued_on <= today), skipped=tuple(skipped))


# ------------------------------------------------------------------ recording a new prescription
RX_SOURCES = ("in_store", "external")
SPH_LIMIT, CYL_LIMIT = 2000, 800          # +/-20.00 D sphere, +/-8.00 D cylinder: beyond these it is a typo, not a prescription
ADD_MIN, ADD_MAX = 50, 400                # reading addition +0.50 to +4.00 D
PD_MIN, PD_MAX = 45, 80                   # binocular pupillary distance, mm


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    month_end = (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, month_end))


def check_new_prescription(od: EyeRx, os: EyeRx, issued_on: date, valid_until: date, today: date, pd: Optional[float]) -> List[str]:
    """Everything wrong with a prescription typed in at the till, as messages a person can act on. Empty = fine.

    Powers are in centi-dioptres (to_cd). The lab works in 0.25 D steps, so anything else is a typo."""
    problems: List[str] = []
    for name, e in (("Right eye (OD)", od), ("Left eye (OS)", os)):
        if e.sph % 25:
            problems.append("%s: SPH %s is not in 0.25 steps." % (name, cd_to_float(e.sph)))
        if abs(e.sph) > SPH_LIMIT:
            problems.append("%s: SPH %s is outside +/-20.00." % (name, cd_to_float(e.sph)))
        if e.cyl % 25:
            problems.append("%s: CYL %s is not in 0.25 steps." % (name, cd_to_float(e.cyl)))
        if abs(e.cyl) > CYL_LIMIT:
            problems.append("%s: CYL %s is outside +/-8.00." % (name, cd_to_float(e.cyl)))
        if e.cyl and e.axis is None:
            problems.append("%s: an axis (1 to 180) is needed when there is cylinder." % name)
        if e.axis is not None and not 1 <= e.axis <= 180:
            problems.append("%s: axis %s must be between 1 and 180." % (name, e.axis))
        if e.add is not None:
            if e.add % 25:
                problems.append("%s: ADD %s is not in 0.25 steps." % (name, cd_to_float(e.add)))
            if not ADD_MIN <= e.add <= ADD_MAX:
                problems.append("%s: ADD %s is outside +0.50 to +4.00." % (name, cd_to_float(e.add)))
    if (od.add is None) != (os.add is None):
        problems.append("ADD is given for one eye only; give it for both eyes, or neither.")
    if pd is not None and not PD_MIN <= pd <= PD_MAX:
        problems.append("PD %s mm is outside %d to %d mm." % (pd, PD_MIN, PD_MAX))
    if issued_on > today:
        problems.append("The test date %s is after today." % issued_on)
    elif valid_until < today:
        problems.append("A prescription from %s expired on %s; it can't be used for a new pair. Book an eye test." % (issued_on, valid_until))
    return problems
