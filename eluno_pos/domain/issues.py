"""Issues raised while building a bill.

BLOCK   - the bill cannot proceed until the cause is fixed (lens outside Rx range, no stock, ...).
CONFIRM - the bill can proceed only if staff explicitly acknowledge the code (recorded in the audit trail).
INFO    - shown to staff, never stops the bill.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

BLOCK, CONFIRM, INFO = "block", "confirm", "info"


@dataclass(frozen=True)
class Issue:
    code: str
    severity: str
    message: str
    pair_ref: Optional[int] = None
    hint: Optional[str] = None

    def to_dict(self) -> dict:
        d = {"code": self.code, "severity": self.severity, "message": self.message}
        if self.pair_ref is not None:
            d["pair_ref"] = self.pair_ref
        if self.hint:
            d["hint"] = self.hint
        return d

    def __str__(self) -> str:
        where = "pair %d: " % self.pair_ref if self.pair_ref is not None else ""
        return "[%s] %s%s%s" % (self.severity.upper(), where, self.message, (" " + self.hint) if self.hint else "")


def blocking(issues: Iterable[Issue], acknowledged: Sequence[str] = ()) -> List[Issue]:
    """Issues that stop the bill: every BLOCK, and every CONFIRM not acknowledged by code."""
    ack = set(acknowledged)
    return [i for i in issues if i.severity == BLOCK or (i.severity == CONFIRM and i.code not in ack)]
