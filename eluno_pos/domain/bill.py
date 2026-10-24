"""The bill: the POS's record of a sale from first quote to Shopify order.

State machine (every transition is persisted before the next side effect):

    DRAFT --start_payment--> PAYMENT_PENDING --mark_paid--> PAID --mark_synced--> SYNCED
      |                         |                            |
      +--cancel--> CANCELLED <--+ (nothing charged)          +--void--> VOIDED (money returned
                                                                         before Shopify; the invoice
                                                                         number stays, as cancelled)

The invoice number is allocated at mark_paid: after the money is captured, before Shopify is
called. A payment that fails never burns a number; a captured payment always has one, even if
Shopify is down, and the order is pushed to Shopify later (outbox) with the invoice number as the
idempotency key.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Dict, List, Optional, Tuple

from .errors import StateConflict
from .pricing import Totals

DRAFT, PAYMENT_PENDING, PAID, SYNCED, CANCELLED, VOIDED = "draft", "payment_pending", "paid", "synced", "cancelled", "voided"
PAYMENT_METHODS = ("upi", "card", "cash", "none")
TERMINAL_METHODS = ("upi", "card")


@dataclass
class BillLine:
    line_ref: str
    pair_ref: int
    kind: str  # frame | lens
    sku: str
    variant_id: int
    title: str
    variant_title: str
    description: str
    hsn: str
    rate_bps: int
    mrp: int
    discounts: List[List[Any]]  # [[offer_id, amount], ...]
    taxable: int
    cgst: int
    sgst: int
    inventory_item_id: Optional[int] = None
    lens_config: Optional[str] = None
    rx_id: Optional[str] = None
    rx_json: Optional[str] = None
    wearer_customer_id: Optional[int] = None
    wearer_name: Optional[str] = None
    wearer_phone: Optional[str] = None
    shopify_line_id: Optional[int] = None

    @property
    def discount(self) -> int:
        return sum(a for _, a in self.discounts)

    @property
    def gross(self) -> int:
        return self.mrp - self.discount

    @property
    def tax(self) -> int:
        return self.cgst + self.sgst

    @property
    def restockable(self) -> bool:
        return self.kind == "frame" and self.inventory_item_id is not None


@dataclass
class Bill:
    bill_id: str
    store_code: str
    location_id: int
    customer_id: int
    customer_name: str
    customer_phone: str
    staff_id: str
    channel: str
    business_date: str
    lines: List[BillLine]
    kind: str = "sale"  # sale | remake
    status: str = DRAFT
    customer_tags: List[str] = field(default_factory=list)
    offers: List[Dict[str, Any]] = field(default_factory=list)  # applied: offer_id, name, amount
    offer_decisions: List[Dict[str, Any]] = field(default_factory=list)
    offer_policy: str = "best_for_customer"
    manual_discount: Optional[Dict[str, Any]] = None
    acknowledged: List[str] = field(default_factory=list)
    notices: List[Dict[str, Any]] = field(default_factory=list)  # non-blocking issues shown at billing
    request: Dict[str, Any] = field(default_factory=dict)
    payment_method: Optional[str] = None
    charge_id: Optional[str] = None
    invoice_no: Optional[str] = None
    paid_at: Optional[str] = None
    shopify_order_id: Optional[int] = None
    shopify_order_name: Optional[str] = None
    shopify_sale_txn_id: Optional[int] = None
    parent_invoice_no: Optional[str] = None
    remake_reason: Optional[str] = None
    remake_cost_bearer: Optional[str] = None
    sync_attempts: int = 0
    last_error: Optional[str] = None
    void_reason: Optional[str] = None
    void_refund_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    source: str = "eluno-pos"  # eluno-pos | imported (an order made before cutover, read from Shopify)

    # ------------------------------------------------------------------ derived
    @property
    def totals(self) -> Totals:
        return Totals(mrp=sum(l.mrp for l in self.lines), discount=sum(l.discount for l in self.lines),
                      net=sum(l.gross for l in self.lines), taxable=sum(l.taxable for l in self.lines),
                      cgst=sum(l.cgst for l in self.lines), sgst=sum(l.sgst for l in self.lines))

    @property
    def total(self) -> int:
        return sum(l.gross for l in self.lines)

    def line(self, line_ref: str) -> BillLine:
        for l in self.lines:
            if l.line_ref == line_ref:
                return l
        raise KeyError(line_ref)

    def frames_needed(self) -> Dict[Tuple[int, int], int]:
        """(inventory_item_id, location_id) -> units this bill takes off the shelf (lenses are made to order)."""
        out: Dict[Tuple[int, int], int] = {}
        for l in self.lines:
            if l.restockable:
                key = (l.inventory_item_id, self.location_id)
                out[key] = out.get(key, 0) + 1  # type: ignore[index]
        return out

    @property
    def rx_ids(self) -> List[str]:
        return sorted({l.rx_id for l in self.lines if l.rx_id})

    # ------------------------------------------------------------------ transitions
    def _require(self, *allowed: str) -> None:
        if self.status not in allowed:
            raise StateConflict("Bill %s is %s; this needs it to be %s." % (self.bill_id, self.status, " or ".join(allowed)),
                                code="bill_state")

    def start_payment(self, method: str) -> None:
        self._require(DRAFT)
        if method not in PAYMENT_METHODS:
            raise StateConflict("unknown payment method %r" % method)
        self.payment_method = method
        self.status = PAYMENT_PENDING

    def payment_failed(self, why: str) -> None:
        self._require(PAYMENT_PENDING)
        self.status = DRAFT
        self.last_error = why

    def mark_paid(self, invoice_no: str, paid_at: str, charge_id: Optional[str] = None) -> None:
        self._require(PAYMENT_PENDING)
        self.invoice_no = invoice_no
        self.paid_at = paid_at
        self.charge_id = charge_id
        self.status = PAID
        self.last_error = None

    def note_sync_attempt(self) -> None:
        self._require(PAID)
        self.sync_attempts += 1

    def sync_failed(self, why: str) -> None:
        self._require(PAID)
        self.last_error = why

    def mark_synced(self, order_id: int, order_name: str, sale_txn_id: Optional[int], line_ids: Dict[str, int]) -> None:
        self._require(PAID)
        missing = [l.line_ref for l in self.lines if l.line_ref not in line_ids]
        if missing:
            raise StateConflict("Shopify order %s is missing lines %s" % (order_id, missing))
        for l in self.lines:
            l.shopify_line_id = line_ids[l.line_ref]
        self.shopify_order_id, self.shopify_order_name, self.shopify_sale_txn_id = order_id, order_name, sale_txn_id
        self.status = SYNCED
        self.last_error = None

    def cancel(self, why: str) -> None:
        """A draft, or a payment that never completed (no charge recorded). Never a bill with money taken."""
        if self.status == PAYMENT_PENDING and self.charge_id:
            raise StateConflict("Bill %s has a terminal charge %s; finish it with `pay`, or void it after." % (self.bill_id, self.charge_id),
                                code="bill_state")
        self._require(DRAFT, PAYMENT_PENDING)
        self.status = CANCELLED
        self.last_error = why

    def void(self, why: str, refund_id: Optional[str]) -> None:
        self._require(PAID)
        self.status = VOIDED
        self.void_reason = why
        self.void_refund_id = refund_id

    # ------------------------------------------------------------------ persistence
    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["total"] = self.total  # derived, for API clients; ignored by from_dict
        d["totals"] = self.totals.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Bill":
        known = {f.name for f in fields(cls)}
        data = {k: v for k, v in d.items() if k in known}
        line_fields = {f.name for f in fields(BillLine)}
        data["lines"] = [BillLine(**{k: v for k, v in l.items() if k in line_fields}) for l in d.get("lines", [])]
        return cls(**data)
