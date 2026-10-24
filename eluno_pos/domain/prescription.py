"""Prescriptions come from the store tool. The till never edits one; it decides whether one is usable."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
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
