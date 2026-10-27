"""Billing use cases: look up, quote, create, pay, sync, cancel, void.

Ordering of side effects in `pay` (each step persisted before the next):
  1. re-price the draft; refuse if anything changed since the customer said yes
  2. charge the terminal with order_ref = bill_id (idempotent by contract); persist the charge id
  3. in one DB transaction: allocate the store's next invoice number, mark the bill paid,
     record a cash sale in the till if cash
  4. create the Shopify order (invoice number = idempotency key); on failure the bill stays PAID
     and `sync` pushes it later. The customer leaves with a valid invoice either way.
"""
from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..adapters.sqlite_repo import SqliteRepository
from ..config import BusinessConfig
from ..domain.bill import CANCELLED, DRAFT, PAID, PAYMENT_PENDING, SYNCED, TERMINAL_METHODS, VOIDED, Bill, BillLine
from ..domain.cart import Cart, CartLine, Pair, stock_issues, structural_issues
from ..domain.catalog import Frame, Lens, LensChoice
from ..domain.documents import CREDIT_NOTE, INVOICE, series_key
from ..domain.errors import BillBlocked, InvalidRequest, NotFound, PaymentFailed, PermissionDenied, StateConflict
from ..domain.issues import Issue, blocking
from ..domain.lens_rules import anomaly_flags, check_lens
from ..domain.offers import ManualDiscount, OfferContext, OfferEvaluation
from ..domain.parties import Customer, Store, normalize_phone, validate_channel
from ..domain.prescription import RxSelection, select_prescription
from ..domain.pricing import PricedLine, Totals, price, totals
from ..ports import CommercePlatform, IntegrationError, PaymentTerminal, PrescriptionSource, UpstreamRejected
from .common import Audit, Clock, Directory, new_id


# ------------------------------------------------------------------ requests
@dataclass(frozen=True)
class PairRequest:
    frame_sku: Optional[str] = None
    lens: Optional[str] = None  # "progressive/1.60/blue_cut"
    wearer_phone: Optional[str] = None  # family billing: someone other than the bill-to customer

    @classmethod
    def parse(cls, text: str) -> "PairRequest":
        """CLI form: 'frame=FR-0003,lens=progressive/1.60/blue_cut,wearer=+919000000004'."""
        kv: Dict[str, str] = {}
        for part in filter(None, (p.strip() for p in text.split(","))):
            if "=" not in part:
                raise InvalidRequest("pair item %r must be key=value (frame=, lens=, wearer=)" % part)
            k, v = part.split("=", 1)
            kv[k.strip()] = v.strip()
        unknown = set(kv) - {"frame", "lens", "wearer"}
        if unknown:
            raise InvalidRequest("unknown pair keys %s; use frame=, lens=, wearer=" % sorted(unknown))
        return cls(kv.get("frame"), kv.get("lens"), kv.get("wearer"))


@dataclass(frozen=True)
class ManualDiscountRequest:
    bps: int
    approver_id: str
    reason_code: str


@dataclass(frozen=True)
class BillRequest:
    store_code: str
    staff_id: str
    customer_phone: str
    channel: str
    pairs: Tuple[PairRequest, ...]
    manual_discount: Optional[ManualDiscountRequest] = None
    acknowledge: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BillRequest":
        md = d.get("manual_discount")
        return cls(store_code=d["store_code"], staff_id=d["staff_id"], customer_phone=d["customer_phone"], channel=d.get("channel", "walk_in"),
                   pairs=tuple(PairRequest(**p) for p in d.get("pairs", [])),
                   manual_discount=ManualDiscountRequest(**md) if md else None, acknowledge=tuple(d.get("acknowledge") or ()))


@dataclass
class Quote:
    request: BillRequest
    store: Store
    customer: Customer
    cart: Cart
    lines: List[CartLine]
    priced: List[PricedLine]
    totals: Totals
    evaluation: OfferEvaluation
    issues: List[Issue]
    blocking: List[Issue]
    stock_levels: Dict[Any, int] = field(default_factory=dict)  # what Shopify said, reused by pay for the reservation

    def to_dict(self) -> Dict[str, Any]:
        by_ref = {p.line_ref: p for p in self.priced}
        return {
            "store_code": self.store.store_code, "customer": {"id": self.customer.customer_id, "name": self.customer.name, "phone": self.customer.phone,
                                                              "flags": self.customer.billing_flags()},
            "lines": [{"line_ref": l.line_ref, "pair_ref": l.pair_ref, "kind": l.kind, "sku": l.item.sku, "description": l.description,  # type: ignore[attr-defined]
                       "wearer": l.wearer.name, "mrp": l.mrp, "discount": by_ref[l.line_ref].discount,
                       "discounts": by_ref[l.line_ref].discounts, "taxable": by_ref[l.line_ref].tax.taxable,
                       "cgst": by_ref[l.line_ref].tax.cgst, "sgst": by_ref[l.line_ref].tax.sgst, "rate_bps": by_ref[l.line_ref].tax.rate_bps,
                       "total": by_ref[l.line_ref].gross} for l in self.lines],
            "totals": self.totals.to_dict(), "offer_policy": self.evaluation.policy,
            "offers": [d.to_dict() for d in self.evaluation.decisions],
            "issues": [i.to_dict() for i in self.issues], "blocked": bool(self.blocking),
        }


class BillingService:
    def __init__(self, commerce: CommercePlatform, prescriptions: PrescriptionSource, terminal: PaymentTerminal,
                 repo: SqliteRepository, config: BusinessConfig, directory: Directory, clock: Clock, audit: Audit):
        self.commerce, self.prescriptions, self.terminal = commerce, prescriptions, terminal
        self.repo, self.config, self.directory, self.clock, self.audit = repo, config, directory, clock, audit
        self._locks: Dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        # Remembered for the length of a visit: who the customer is and which prescription applies don't change
        # while they choose frames, so re-pricing only re-asks Shopify for stock. Stock is never remembered.
        self._visit_seconds = float(config.setting("visit_cache_seconds", 900))  # type: ignore[arg-type]
        self._visit: Dict[Any, Tuple[float, Any]] = {}

    def _bill_lock(self, bill_id: str) -> threading.Lock:
        """One payment attempt per bill at a time (a double-clicked Pay must not charge twice)."""
        with self._locks_guard:
            return self._locks.setdefault(bill_id, threading.Lock())

    # ------------------------------------------------------------------ lookup
    def _remembered(self, key: Any, fetch, fresh: bool = False):
        hit = self._visit.get(key)
        if not fresh and hit is not None and time.monotonic() - hit[0] < self._visit_seconds:
            return hit[1]
        value = fetch()
        if value is not None:
            self._visit[key] = (time.monotonic(), value)
        return value

    def customer(self, phone: str, fresh: bool = False) -> Customer:
        p = normalize_phone(phone)
        c = self._remembered(("customer", p), lambda: self.commerce.find_customer_by_phone(p), fresh)
        if c is None:
            raise NotFound("No customer with phone %s in Shopify. Add them as a new customer." % p, code="customer_not_found")
        return c

    def rx_selection(self, phone: str, fresh: bool = False) -> RxSelection:
        today = self.clock.today()  # keyed by date: a prescription that expires at midnight is re-judged next morning
        return self._remembered(("rx", phone, today), lambda: select_prescription(self.prescriptions.prescriptions(phone), today), fresh)

    def lookup(self, phone: str) -> Dict[str, Any]:
        """Start of a visit: always read fresh from Shopify and the store tool, then remember for the visit."""
        c = self.customer(phone, fresh=True)
        sel = self.rx_selection(c.phone, fresh=True)
        rx = sel.chosen
        return {
            "customer": {"id": c.customer_id, "name": c.name, "phone": c.phone, "flags": c.billing_flags(), "tags": sorted(c.tags),
                         "lifetime_value": c.lifetime_value},
            "prescription": None if rx is None else {
                "rx_id": rx.rx_id, "issued_on": rx.issued_on.isoformat(), "valid_until": rx.valid_until.isoformat(), "source": rx.source,
                "od": asdict(rx.od), "os": asdict(rx.os), "pd": rx.pd, "notes": rx.notes},
            "skipped_prescriptions": [{"rx_id": s.rx_id, "reason": s.reason} for s in sel.skipped],
            "flags": [i.to_dict() for i in anomaly_flags(None, sel)],
        }

    # ------------------------------------------------------------------ quote
    def quote(self, req: BillRequest, exclude_bill: Optional[str] = None) -> Quote:
        """Price a cart. `exclude_bill` is the bill being re-priced, so its own stock hold is not counted against it."""
        store = self.directory.store(req.store_code)
        staff = self.directory.staff(store, req.staff_id)
        validate_channel(req.channel)
        customer = self.customer(req.customer_phone)
        if not req.pairs:
            raise InvalidRequest("Add at least one pair (frame and/or lens).", code="cart_empty")
        catalog = self.commerce.catalog()
        today = self.clock.today()
        wearers: Dict[str, Customer] = {customer.phone: customer}
        selections: Dict[str, RxSelection] = {}
        pairs: List[Pair] = []
        issues: List[Issue] = []
        for i, pr in enumerate(req.pairs, start=1):
            wearer = customer
            if pr.wearer_phone:
                wp = normalize_phone(pr.wearer_phone)
                wearer = wearers.get(wp) or self.customer(wp)
                wearers[wp] = wearer
            frame: Optional[Frame] = catalog.frame(pr.frame_sku) if pr.frame_sku else None
            try:
                lens: Optional[Lens] = catalog.lens(LensChoice.parse(pr.lens)) if pr.lens else None
            except ValueError as e:
                raise InvalidRequest("Pair %d lens %r: %s." % (i, pr.lens, e), code="bad_lens")
            sel = RxSelection(chosen=None)
            if lens is not None:
                if wearer.phone not in selections:
                    selections[wearer.phone] = self.rx_selection(wearer.phone)
                sel = selections[wearer.phone]
                issues += check_lens(lens, sel, today, catalog, i) + anomaly_flags(lens, sel, staff, i)
            pairs.append(Pair(i, wearer, sel, frame, lens))
        cart = Cart(store, customer, staff, req.channel, today, tuple(pairs))
        issues = structural_issues(cart) + issues
        frame_items = [p.frame.inventory_item_id for p in pairs if p.frame and p.frame.inventory_item_id]
        levels: Dict[Any, int] = {}
        if frame_items:
            held = self.repo.holds([(i, store.location_id) for i in frame_items], exclude_bill=exclude_bill)
            levels = self.commerce.stock_levels(frame_items)
            issues += stock_issues(cart, levels, self.directory.stores_by_location(), held)
        manual = None
        if req.manual_discount:
            md = req.manual_discount
            approver = self.directory.staff(store, md.approver_id)
            manual = ManualDiscount(str(self.config.setting("manual_discount_offer_id", "MANAGER_OVERRIDE")), md.bps, approver, md.reason_code)
        lines = cart.lines()
        evaluation = self.config.offer_engine().evaluate([l.offer_line() for l in lines],
                                                         OfferContext(today, customer.tags, store.store_code, manual))
        priced = price([(l.line_ref, l.mrp, l.hsn) for l in lines], evaluation, self.config.taxes, today)
        unknown_acks = set(req.acknowledge) - {i.code for i in issues}
        if unknown_acks:
            raise InvalidRequest("Acknowledged %s, but no such issue is raised on this bill." % ", ".join(sorted(unknown_acks)), code="bad_acknowledgement")
        return Quote(req, store, customer, cart, lines, priced, totals(priced), evaluation, issues, blocking(issues, req.acknowledge), levels)

    # ------------------------------------------------------------------ create
    def create(self, req: BillRequest) -> Tuple[Bill, Quote]:
        q = self.quote(req)
        if q.blocking:
            raise BillBlocked("The bill cannot be created: %d issue(s) to fix." % len(q.blocking), q.blocking)
        now = self.clock.iso()
        by_ref = {p.line_ref: p for p in q.priced}
        lines = []
        for l in q.lines:
            p = by_ref[l.line_ref]
            rx = l.rx_selection.chosen if l.kind == "lens" else None
            item = l.item
            lines.append(BillLine(
                line_ref=l.line_ref, pair_ref=l.pair_ref, kind=l.kind, sku=item.sku, variant_id=item.variant_id,  # type: ignore[attr-defined]
                title=item.title, variant_title=item.variant_title if l.kind == "frame" else item.coating,  # type: ignore[attr-defined]
                description=l.description, hsn=l.hsn, rate_bps=p.tax.rate_bps, mrp=p.mrp, discounts=[[o, a] for o, a in p.discounts],
                taxable=p.tax.taxable, cgst=p.tax.cgst, sgst=p.tax.sgst,
                inventory_item_id=item.inventory_item_id if l.kind == "frame" else None,  # type: ignore[attr-defined]
                lens_config=item.config_code if l.kind == "lens" else None,  # type: ignore[attr-defined]
                rx_id=rx.rx_id if rx else None, rx_json=rx.line_property() if rx else None,
                wearer_customer_id=l.wearer.customer_id, wearer_name=l.wearer.name, wearer_phone=l.wearer.phone))
        md = req.manual_discount
        manual_applied = bool(md and q.evaluation.manual and any(d.offer_id == q.evaluation.manual.offer_id for d in q.evaluation.applied))
        bill = Bill(
            bill_id=new_id("bill"), store_code=q.store.store_code, location_id=q.store.location_id, customer_id=q.customer.customer_id,
            customer_name=q.customer.name, customer_phone=q.customer.phone, staff_id=req.staff_id, channel=req.channel,
            business_date=self.clock.today().isoformat(), lines=lines, customer_tags=sorted(q.customer.tags),
            offers=[{"offer_id": d.offer_id, "name": d.name, "amount": d.amount} for d in q.evaluation.applied],
            offer_decisions=[d.to_dict() for d in q.evaluation.decisions], offer_policy=q.evaluation.policy,
            manual_discount=({"offer_id": q.evaluation.manual.offer_id, "bps": md.bps, "approver_id": md.approver_id, "reason_code": md.reason_code}  # type: ignore[union-attr]
                             if manual_applied else None),
            acknowledged=list(req.acknowledge), notices=[i.to_dict() for i in q.issues if i.severity != "block"],
            request=req.to_dict(), created_at=now, updated_at=now)
        self.repo.save_bill(bill)
        self.audit(req.staff_id, "bill.created", bill.bill_id, bill.store_code, total=bill.total, offers=[o["offer_id"] for o in bill.offers])
        if md:  # audited whether or not it won against the automatic offers
            self.audit(md.approver_id, "discount.manual_override", bill.bill_id, bill.store_code, md.reason_code,
                       bps=md.bps, requested_by=req.staff_id, applied=manual_applied)
        for code in req.acknowledge:
            self.audit(req.staff_id, "issue.acknowledged", bill.bill_id, bill.store_code, code=code)
        return bill, q

    # ------------------------------------------------------------------ pay
    def get(self, bill_id: str) -> Bill:
        b = self.repo.get_bill(bill_id)
        if b is None:
            raise NotFound("No bill %s." % bill_id, code="bill_not_found")
        return b

    def _save(self, bill: Bill) -> None:
        bill.updated_at = self.clock.iso()
        self.repo.save_bill(bill)

    def ensure_series(self, store: Store, kind: str) -> str:
        """A store's series continues after the highest number Shopify POS issued before cutover.

        Normally done once by `cutover`; done lazily as a fallback, but always before money moves, so a
        Shopify outage can never leave a captured payment without an invoice number."""
        series = series_key(store.invoice_prefix or "", kind)
        if not self.repo.series_exists(series):
            try:
                floor = self.commerce.highest_document_seq(store, series)
            except IntegrationError as e:
                raise StateConflict("The %s series for %s is not initialised and Shopify is unreachable (%s). Nothing was charged. "
                                    "Run `cutover` once Shopify is back." % (kind.replace("_", " "), store.store_code, e), code="series_not_ready")
            self.repo.init_series(series, floor, self.clock.iso())
            self.audit("system", "series.initialised", series, store.store_code, floor=floor)
        return series

    def cutover(self, store_codes: Sequence[str] = ()) -> Dict[str, Dict[str, int]]:
        """Go-live step per store: start the invoice and credit-note series after Shopify POS's last numbers."""
        out = {}
        for s in self.directory.stores():
            if s.is_billing_store and (not store_codes or s.store_code in store_codes):
                for kind in (INVOICE, CREDIT_NOTE):
                    series = self.ensure_series(s, kind)
                    out[series] = self.repo.series_info(series)
        return out

    def pay(self, bill_id: str, method: str, staff_id: Optional[str] = None) -> Bill:
        with self._bill_lock(bill_id):
            return self._pay(bill_id, method, staff_id)

    def _pay(self, bill_id: str, method: str, staff_id: Optional[str] = None) -> Bill:
        started = time.monotonic()
        bill = self.get(bill_id)
        if bill.status in (SYNCED,):
            return bill  # the till retried after a dropped connection: nothing to do
        if bill.status == PAID:
            return self.sync(bill)
        if bill.status in (CANCELLED, VOIDED):
            raise StateConflict("Bill %s is %s." % (bill.bill_id, bill.status), code="bill_state")
        store = self.directory.store(bill.store_code)
        if bill.status == DRAFT:
            method = "none" if bill.total == 0 else method
            if method not in ("upi", "card", "cash", "none") or (method == "none" and bill.total > 0):
                raise InvalidRequest("Payment method must be upi, card or cash.", code="bad_payment_method")
            fresh = self._recheck(bill) if bill.kind == "sale" else None
            if method == "cash" and self.repo.open_till(store.store_code) is None:
                raise StateConflict("No open till at %s. Open the till (with its float) before taking cash." % store.store_code, code="till_not_open")
            self.ensure_series(store, INVOICE)  # before any money moves
            wanted = bill.frames_needed()
            levels = (fresh.stock_levels if fresh is not None else self.commerce.stock_levels([item for item, _ in wanted])) if wanted else {}
            with self.repo.transaction():  # hold the frames and start payment together: two tills cannot take the last unit
                if wanted:
                    self.repo.hold_stock(bill.bill_id, wanted, levels, self.clock.iso())
                bill.start_payment(method)
                self._save(bill)
            self.audit(staff_id or bill.staff_id, "payment.started", bill.bill_id, bill.store_code, method=method, amount=bill.total,
                       frames_held=sum(wanted.values()))

        if bill.payment_method in TERMINAL_METHODS and not bill.charge_id:
            try:
                charge = self.terminal.charge(bill.total, bill.payment_method, order_ref=bill.bill_id)
            except UpstreamRejected as e:
                bill.payment_failed(str(e))
                self._save(bill)
                # the frame stays reserved: the customer is still at the counter with it and may pay another way;
                # it is released only when the bill is paid or cancelled
                self.audit(staff_id or bill.staff_id, "payment.declined", bill.bill_id, bill.store_code, error=str(e))
                raise PaymentFailed("The terminal declined the payment (%s). Nothing was charged; the frame stays reserved for this bill. "
                                    "Try again, use another method, or cancel the bill to release it." % e, code="payment_declined")
            except IntegrationError as e:
                bill.last_error = str(e)
                self._save(bill)
                raise PaymentFailed("Could not reach the payment terminal (%s). Retry payment: the same bill reference is reused, "
                                    "so the customer cannot be charged twice." % e, code="terminal_unreachable")
            bill.charge_id = charge.charge_id
            self._save(bill)  # persisted before anything else can fail

        series = self.ensure_series(store, INVOICE)
        with self.repo.transaction():
            now = self.clock.iso()
            invoice_no = self.repo.allocate(series, INVOICE, store.store_code, bill.bill_id, now)
            bill.mark_paid(invoice_no, now, bill.charge_id)
            if bill.payment_method == "cash":
                till = self.repo.open_till(store.store_code)
                if till is None:
                    raise StateConflict("The till at %s was closed during payment; open it and retry." % store.store_code, code="till_not_open")
                till.record("cash_sale", bill.total, invoice_no, now, staff_id or bill.staff_id)
                self.repo.save_till(till)
            self._save(bill)
            self.audit(staff_id or bill.staff_id, "bill.paid", bill.bill_id, bill.store_code, invoice_no=invoice_no,
                       method=bill.payment_method, amount=bill.total, charge_id=bill.charge_id)
        bill = self.sync(bill)
        self.audit("system", "bill.timing", bill.bill_id, bill.store_code, seconds=round(time.monotonic() - started, 3))
        return bill

    def _recheck(self, bill: Bill) -> "Quote":
        """The customer agreed to a price. If stock, offers or tax changed since, do not charge silently."""
        q = self.quote(BillRequest.from_dict(bill.request), exclude_bill=bill.bill_id)
        if q.blocking:
            raise BillBlocked("The bill can no longer be paid as quoted.", q.blocking)
        now = {p.line_ref: (p.mrp, p.discount, p.tax.cgst, p.tax.sgst) for p in q.priced}
        then = {l.line_ref: (l.mrp, l.discount, l.cgst, l.sgst) for l in bill.lines}
        if now != then:
            raise StateConflict("Prices, offers or tax changed since this bill was quoted (was Rs %s, now Rs %s). Create a new bill and confirm with the customer." % (
                _rs(bill.total), _rs(q.totals.net)), code="quote_changed")
        return q

    # ------------------------------------------------------------------ sync (outbox)
    def sync(self, bill: Bill) -> Bill:
        if bill.status != PAID:
            return bill
        store = self.directory.store(bill.store_code)
        may_exist = bill.sync_attempts > 0
        bill.note_sync_attempt()
        self._save(bill)  # write-ahead: a crash mid-call makes the next attempt look before it creates
        try:
            receipt = self.commerce.create_order(bill, store, may_exist=may_exist)
        except IntegrationError as e:
            bill.sync_failed(str(e))
            self._save(bill)
            self.audit("system", "bill.sync_failed", bill.bill_id, bill.store_code, invoice_no=bill.invoice_no, error=str(e), retryable=e.retryable)
            return bill
        bill.mark_synced(receipt.order_id, receipt.order_name, receipt.sale_txn_id, receipt.line_ids)
        with self.repo.transaction():
            self._save(bill)
            self.repo.release_stock(bill.bill_id)  # Shopify's count now includes this sale
        self.audit("system", "bill.synced", bill.bill_id, bill.store_code, invoice_no=bill.invoice_no, order=receipt.order_name,
                   adopted_existing=receipt.adopted)
        return bill

    def pending(self) -> List[Bill]:
        return self.repo.bills(statuses=[PAID, PAYMENT_PENDING])

    # ------------------------------------------------------------------ cancel / void
    def cancel(self, bill_id: str, staff_id: str, why: str = "customer did not proceed", approver_id: Optional[str] = None) -> Bill:
        """Cancel a draft, or a payment that never completed. Releases the frames it was holding.

        A bill stuck in PAYMENT_PENDING (terminal stopped answering) can only be cancelled by a store manager
        who has checked the terminal shows no payment: if a charge did go through after all, it carries this
        bill id as its order_ref and has to be refunded on the terminal."""
        bill = self.get(bill_id)
        store = self.directory.store(bill.store_code)
        self.directory.staff(store, staff_id)
        unresolved = bill.status == PAYMENT_PENDING
        if unresolved:
            if not approver_id:
                raise PermissionDenied("Bill %s is waiting on the terminal. A store manager must confirm the terminal shows no payment, "
                                       "then cancel it (approver needed)." % bill.bill_id, code="approver_required")
            self.directory.staff(store, approver_id, role="store_manager")
        bill.cancel(why)
        with self.repo.transaction():
            self._save(bill)
            released = self.repo.release_stock(bill.bill_id)
        self.audit(approver_id or staff_id, "bill.cancelled", bill.bill_id, bill.store_code, why=why, requested_by=staff_id,
                   unresolved_payment=unresolved, frames_released=released)
        return bill

    def void(self, bill_id: str, staff_id: str, approver_id: str, reason_code: str) -> Bill:
        """A paid bill that cannot reach Shopify: give the money back and cancel the invoice number."""
        self.config.require_reason("void", reason_code)
        bill = self.get(bill_id)
        if bill.status != PAID:
            raise StateConflict("Only a paid bill that has not reached Shopify can be voided; %s is %s. Use a refund instead." % (
                bill.invoice_no or bill.bill_id, bill.status), code="bill_state")
        store = self.directory.store(bill.store_code)
        self.directory.staff(store, approver_id, role="store_manager")
        refund_ref = None
        if bill.payment_method in TERMINAL_METHODS and bill.charge_id:
            refund_ref = self.terminal.refund(bill.charge_id, bill.total, reason_code).refund_id
        with self.repo.transaction():
            now = self.clock.iso()
            if bill.payment_method == "cash":
                till = self.repo.open_till(store.store_code)
                if till is None:
                    raise StateConflict("Open the till to return cash.", code="till_not_open")
                till.record("cash_refund", bill.total, bill.invoice_no or bill.bill_id, now, staff_id, reason=reason_code)
                self.repo.save_till(till)
            bill.void(reason_code, refund_ref)
            self._save(bill)
            self.repo.release_stock(bill.bill_id)  # the sale is undone; the frame goes back on the shelf
            self.repo.cancel_document(bill.invoice_no or "", "voided: %s" % reason_code)
            self.audit(approver_id, "bill.voided", bill.bill_id, bill.store_code, reason_code, invoice_no=bill.invoice_no, requested_by=staff_id)
        return bill


def _rs(paise: int) -> str:
    return "%d.%02d" % (paise // 100, paise % 100)
