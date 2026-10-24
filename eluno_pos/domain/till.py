"""Cash drawer sessions. One open session per store; every rupee in or out of the drawer is a movement.

expected = opening float + cash sales - cash refunds + paid-ins - paid-outs
variance = counted - expected   (negative = short)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional

from .errors import InvalidRequest, StateConflict

OPEN, CLOSED = "open", "closed"
SIGN = {"cash_sale": 1, "cash_refund": -1, "paid_in": 1, "paid_out": -1}
NEEDS_REASON = ("paid_in", "paid_out")


@dataclass
class TillMovement:
    type: str
    amount_paise: int  # signed: what it does to the drawer
    ref: str
    at: str
    staff_id: str
    reason: Optional[str] = None
    note: Optional[str] = None


@dataclass
class TillSession:
    session_id: str
    store_code: str
    location_id: int
    opened_at: str
    opened_by: str
    opening_float_paise: int
    movements: List[TillMovement] = field(default_factory=list)
    status: str = OPEN
    closed_at: Optional[str] = None
    closed_by: Optional[str] = None
    counted_cash_paise: Optional[int] = None
    variance_note: Optional[str] = None
    variance_flagged: bool = False

    @property
    def expected_cash_paise(self) -> int:
        return self.opening_float_paise + sum(m.amount_paise for m in self.movements)

    @property
    def variance_paise(self) -> Optional[int]:
        return None if self.counted_cash_paise is None else self.counted_cash_paise - self.expected_cash_paise

    def total(self, kind: str) -> int:
        return sum(m.amount_paise for m in self.movements if m.type == kind)

    def record(self, kind: str, amount: int, ref: str, at: str, staff_id: str, reason: Optional[str] = None,
               note: Optional[str] = None) -> TillMovement:
        if self.status != OPEN:
            raise StateConflict("Till %s is closed." % self.session_id, code="till_closed")
        if kind not in SIGN:
            raise InvalidRequest("unknown till movement %r" % kind)
        if not isinstance(amount, int) or amount <= 0:
            raise InvalidRequest("Amount must be a positive number of paise.", code="bad_amount")
        if kind in NEEDS_REASON and not reason:
            raise InvalidRequest("A %s needs a reason code." % kind.replace("_", "-"), code="reason_code_required")
        signed = SIGN[kind] * amount
        if self.expected_cash_paise + signed < 0:
            raise StateConflict("The drawer should only hold Rs %d.%02d; it cannot pay out Rs %d.%02d." % (
                self.expected_cash_paise // 100, self.expected_cash_paise % 100, amount // 100, amount % 100), code="drawer_negative")
        m = TillMovement(kind, signed, ref, at, staff_id, reason, note)
        self.movements.append(m)
        return m

    def close(self, counted: int, at: str, staff_id: str, alert_threshold: int, note: Optional[str] = None) -> None:
        if self.status != OPEN:
            raise StateConflict("Till %s is already closed." % self.session_id, code="till_closed")
        if not isinstance(counted, int) or counted < 0:
            raise InvalidRequest("Counted cash must be zero or more paise.", code="bad_amount")
        self.counted_cash_paise = counted
        self.closed_at, self.closed_by, self.status = at, staff_id, CLOSED
        self.variance_flagged = abs(self.variance_paise or 0) > alert_threshold
        if self.variance_flagged and not note:
            note = "Variance over threshold; manager to review."
        self.variance_note = note

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["expected_cash_paise"] = self.expected_cash_paise
        d["variance_paise"] = self.variance_paise
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TillSession":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["movements"] = [TillMovement(**m) for m in d.get("movements", [])]
        return cls(**data)
