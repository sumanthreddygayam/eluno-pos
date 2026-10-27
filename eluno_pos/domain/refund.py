"""Refunds. A refund reverses whole lines of a synced bill and is documented by a credit note.

Rules:
  * Tax reversed per line is exactly the tax charged on that line, not recomputed at today's rate
    (a rate change between sale and refund must not change the credit note).
  * Frames go back into stock at the store that billed them; lenses are made to order and are scrapped.
  * A line can be refunded once. Full refund = every line not yet refunded.
  * Money first (terminal or till), then the credit note number, then Shopify, each step persisted.

    REQUESTED --money returned--> MONEY_RETURNED --Shopify refund created--> SYNCED
        +--money could not be returned--> FAILED
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .bill import SYNCED as BILL_SYNCED, Bill
from .errors import InvalidRequest, StateConflict

REQUESTED, MONEY_RETURNED, SYNCED, FAILED = "requested", "money_returned", "synced", "failed"


@dataclass
class RefundLine:
    line_ref: str
    kind: str
    sku: str
    shopify_line_id: Optional[int]
    gross: int
    taxable: int
    cgst: int
    sgst: int
    restock: bool
    location_id: Optional[int]

    @property
    def tax(self) -> int:
        return self.cgst + self.sgst


@dataclass
class Refund:
    refund_id: str
    bill_id: str
    invoice_no: str
    store_code: str
    location_id: int
    lines: List[RefundLine]
    reason_code: str
    staff_id: str
    method: str
    created_at: str
    status: str = REQUESTED
    approver_id: Optional[str] = None
    note: Optional[str] = None
    credit_note_no: Optional[str] = None
    terminal_refund_id: Optional[str] = None
    money_returned_at: Optional[str] = None
    shopify_refund_id: Optional[int] = None
    sync_attempts: int = 0
    last_error: Optional[str] = None

    @property
    def amount(self) -> int:
        return sum(l.gross for l in self.lines)

    @property
    def taxable(self) -> int:
        return sum(l.taxable for l in self.lines)

    @property
    def tax(self) -> int:
        return sum(l.tax for l in self.lines)

    def _require(self, *allowed: str) -> None:
        if self.status not in allowed:
            raise StateConflict("Refund %s is %s; this needs %s." % (self.refund_id, self.status, " or ".join(allowed)), code="refund_state")

    def mark_money_returned(self, credit_note_no: str, at: str, terminal_refund_id: Optional[str]) -> None:
        self._require(REQUESTED)
        self.credit_note_no, self.money_returned_at, self.terminal_refund_id = credit_note_no, at, terminal_refund_id
        self.status = MONEY_RETURNED
        self.last_error = None

    def fail(self, why: str) -> None:
        self._require(REQUESTED)
        self.status = FAILED
        self.last_error = why

    def note_sync_attempt(self) -> None:
        self._require(MONEY_RETURNED)
        self.sync_attempts += 1

    def mark_synced(self, shopify_refund_id: int) -> None:
        self._require(MONEY_RETURNED)
        self.shopify_refund_id = shopify_refund_id
        self.status = SYNCED
        self.last_error = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d.update(amount=self.amount, taxable=self.taxable, tax=self.tax)  # derived, for API clients; ignored by from_dict
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Refund":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        data["lines"] = [RefundLine(**l) for l in d.get("lines", [])]
        return cls(**data)


def refunded_line_refs(refunds: Iterable[Refund]) -> set:
    """Lines already refunded or with a refund in flight."""
    return {l.line_ref for r in refunds if r.status != FAILED for l in r.lines}


def plan_refund(bill: Bill, line_refs: Optional[Sequence[str]], already_refunded: set) -> List[RefundLine]:
    if bill.status != BILL_SYNCED:
        raise StateConflict("Bill %s is %s; only bills that reached Shopify can be refunded (run sync first)." % (bill.invoice_no or bill.bill_id, bill.status),
                            code="bill_not_synced")
    if line_refs:
        unknown = [r for r in line_refs if r not in {l.line_ref for l in bill.lines}]
        if unknown:
            raise InvalidRequest("Invoice %s has no line %s. Lines: %s." % (bill.invoice_no, ", ".join(unknown), ", ".join(l.line_ref for l in bill.lines)),
                                 code="unknown_line")
        dup = [r for r in line_refs if r in already_refunded]
        if dup:
            raise StateConflict("Line %s of %s is already refunded." % (", ".join(dup), bill.invoice_no), code="already_refunded")
        chosen = [bill.line(r) for r in dict.fromkeys(line_refs)]
    else:
        chosen = [l for l in bill.lines if l.line_ref not in already_refunded]
        if not chosen:
            raise StateConflict("Invoice %s is already fully refunded." % bill.invoice_no, code="already_refunded")
    if sum(l.gross for l in chosen) == 0:
        raise InvalidRequest("Nothing to refund: the selected lines were billed at zero.", code="zero_refund")
    return [RefundLine(line_ref=l.line_ref, kind=l.kind, sku=l.sku, shopify_line_id=l.shopify_line_id, gross=l.gross,
                       taxable=l.taxable, cgst=l.cgst, sgst=l.sgst, restock=l.restockable,
                       location_id=bill.location_id if l.restockable else None)
            for l in chosen]
