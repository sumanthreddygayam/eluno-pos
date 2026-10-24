"""Refunds (full or partial) through Shopify's refund flow, documented by a credit note.

Steps, each persisted before the next:
  1. checks that need nothing to move: lines refundable, Shopify agrees (refunds/calculate), the
     drawer can cover a cash refund, the credit-note series is ready
  2. REQUESTED is saved; terminal refund (card/UPI) and its id persisted at once
  3. one DB transaction: credit-note number + cash-out movement for cash -> MONEY_RETURNED
  4. Shopify refund (credit-note number = idempotency key) -> SYNCED; on failure `sync` retries
A refund interrupted after the terminal paid out is resumed by `sync`; one where no money moved is FAILED.
"""
from __future__ import annotations

from typing import List, Optional, Sequence

from ..adapters.sqlite_repo import SqliteRepository
from ..config import BusinessConfig
from ..domain.bill import TERMINAL_METHODS, Bill
from ..domain.documents import CREDIT_NOTE
from ..domain.errors import DomainError, InvalidRequest, NotFound, PaymentFailed, StateConflict
from ..domain.parties import Store
from ..domain.refund import MONEY_RETURNED, REQUESTED, Refund, plan_refund, refunded_line_refs
from ..ports import CommercePlatform, IntegrationError, PaymentTerminal, UpstreamRejected
from .billing import BillingService
from .common import Audit, Clock, Directory, new_id


class RefundService:
    def __init__(self, commerce: CommercePlatform, terminal: PaymentTerminal, repo: SqliteRepository, config: BusinessConfig,
                 directory: Directory, clock: Clock, audit: Audit, billing: BillingService):
        self.commerce, self.terminal, self.repo, self.config = commerce, terminal, repo, config
        self.directory, self.clock, self.audit, self.billing = directory, clock, audit, billing

    def find_bill(self, invoice_no: str, store: Store) -> Bill:
        """Bills made by this POS are local; bills from before cutover are read from Shopify once and kept."""
        bill = self.repo.get_bill_by_invoice(invoice_no)
        if bill is None:
            bill = self.commerce.find_order(invoice_no, store)
            if bill is None:
                raise NotFound("No invoice %s at %s, locally or in Shopify." % (invoice_no, store.store_code), code="invoice_not_found")
            self.repo.save_bill(bill)
            self.audit("system", "bill.imported", bill.bill_id, store.store_code, invoice_no=invoice_no, order=bill.shopify_order_name)
        return bill

    def refund(self, invoice_no: str, store_code: str, staff_id: str, reason_code: str,
               line_refs: Optional[Sequence[str]] = None, approver_id: Optional[str] = None, note: Optional[str] = None) -> Refund:
        store = self.directory.store(store_code)
        self.directory.staff(store, staff_id)
        if approver_id:
            self.directory.staff(store, approver_id, role="store_manager")
        self.config.require_reason("refund", reason_code)
        bill = self.find_bill(invoice_no, store)
        if bill.store_code != store.store_code:
            raise StateConflict("%s was billed at %s. Refund it there: the frame restocks there and that till took the money." % (invoice_no, bill.store_code),
                                code="wrong_store")
        if bill.payment_method == "none" or bill.total == 0:
            raise InvalidRequest("%s was billed at zero; there is nothing to refund." % invoice_no, code="zero_refund")
        lines = plan_refund(bill, line_refs, refunded_line_refs(self.repo.refunds(bill_id=bill.bill_id)))
        method = bill.payment_method or "cash"
        refund = Refund(refund_id=new_id("rfd"), bill_id=bill.bill_id, invoice_no=invoice_no, store_code=store.store_code,
                        location_id=store.location_id, lines=lines, reason_code=reason_code, staff_id=staff_id, method=method,
                        created_at=self.clock.iso(), approver_id=approver_id, note=note)

        # 1. everything that can refuse, before anything is recorded or moved
        if method == "cash":
            till = self.repo.open_till(store.store_code)
            if till is None:
                raise StateConflict("Cash refund needs an open till at %s." % store.store_code, code="till_not_open")
            if till.expected_cash_paise < refund.amount:
                raise StateConflict("The drawer should hold Rs %s; this cash refund is Rs %s. Record a paid-in first or refund another way." % (
                    _rs(till.expected_cash_paise), _rs(refund.amount)), code="drawer_negative")
        elif method in TERMINAL_METHODS and not bill.charge_id:
            raise StateConflict("%s has no terminal charge on record; refund it on the original gateway." % invoice_no, code="no_charge")
        self.billing.ensure_series(store, CREDIT_NOTE)
        try:
            quote = self.commerce.quote_refund(bill, refund)
        except UpstreamRejected as e:
            raise StateConflict("Shopify will not accept this refund: %s" % e, code="refund_rejected")
        if quote.maximum_refundable < refund.amount:
            raise StateConflict("Shopify shows Rs %s refundable on %s; this refund is Rs %s." % (_rs(quote.maximum_refundable), invoice_no, _rs(refund.amount)),
                                code="over_refund")
        mismatched = [l.line_ref for l in lines if l.shopify_line_id in quote.lines and quote.lines[l.shopify_line_id] != (l.taxable, l.tax)]

        # 2. record intent, then move terminal money and persist its id at once
        self.repo.save_refund(refund)
        self.audit(staff_id, "refund.requested", refund.refund_id, store.store_code, reason_code, invoice_no=invoice_no,
                   lines=[l.line_ref for l in lines], amount=refund.amount, approver=approver_id, shopify_tax_mismatch=mismatched)
        if method in TERMINAL_METHODS:
            try:
                refund.terminal_refund_id = self.terminal.refund(bill.charge_id or "", refund.amount, reason_code).refund_id
            except IntegrationError as e:
                refund.fail(str(e))
                self.repo.save_refund(refund)
                self.audit("system", "refund.failed", refund.refund_id, store.store_code, error=str(e))
                raise PaymentFailed("The terminal could not refund %s: %s. No credit note was issued." % (invoice_no, e), code="refund_failed")
            self.repo.save_refund(refund)
        return self._complete(refund, store)

    def _complete(self, refund: Refund, store: Store) -> Refund:
        """Credit note + drawer movement in one transaction, then Shopify."""
        series = self.billing.ensure_series(store, CREDIT_NOTE)
        try:
            with self.repo.transaction():
                now = self.clock.iso()
                cn = self.repo.allocate(series, CREDIT_NOTE, store.store_code, refund.refund_id, now)
                refund.mark_money_returned(cn, now, refund.terminal_refund_id)
                if refund.method == "cash":
                    till = self.repo.open_till(store.store_code)
                    if till is None:
                        raise StateConflict("The till was closed during the refund; open it and refund again.", code="till_not_open")
                    till.record("cash_refund", refund.amount, cn, now, refund.staff_id, reason=refund.reason_code)
                    self.repo.save_till(till)
                self.repo.save_refund(refund)
                self.audit(refund.staff_id, "refund.money_returned", refund.refund_id, store.store_code, refund.reason_code, credit_note_no=cn,
                           amount=refund.amount, method=refund.method, terminal_refund_id=refund.terminal_refund_id)
        except DomainError as e:
            persisted = self.repo.get_refund(refund.refund_id)  # the rollback undid the DB writes; drop the in-memory changes too
            if persisted is not None and persisted.method == "cash" and persisted.status == REQUESTED:
                persisted.fail("no cash paid out: %s" % e.message)
                self.repo.save_refund(persisted)
            raise
        return self.sync(refund)

    def sync(self, refund: Refund) -> Refund:
        if refund.status != MONEY_RETURNED:
            return refund
        bill = self.repo.get_bill(refund.bill_id)
        if bill is None:
            raise NotFound("bill %s for refund %s is missing" % (refund.bill_id, refund.refund_id))
        may_exist = refund.sync_attempts > 0
        refund.note_sync_attempt()
        self.repo.save_refund(refund)
        reason = self.config.reason_codes.get("refund", {}).get(refund.reason_code, refund.reason_code)
        try:
            receipt = self.commerce.create_refund(bill, refund, may_exist, reason_text="%s: %s" % (refund.reason_code, reason))
        except IntegrationError as e:
            refund.last_error = str(e)
            self.repo.save_refund(refund)
            self.audit("system", "refund.sync_failed", refund.refund_id, refund.store_code, error=str(e))
            return refund
        refund.mark_synced(receipt.refund_id)
        self.repo.save_refund(refund)
        self.audit("system", "refund.synced", refund.refund_id, refund.store_code, credit_note_no=refund.credit_note_no,
                   shopify_refund_id=receipt.refund_id, adopted_existing=receipt.adopted)
        return refund

    def resume(self, refund: Refund) -> Refund:
        """Finish a refund interrupted mid-way (crash, closed laptop)."""
        if refund.status == MONEY_RETURNED:
            return self.sync(refund)
        if refund.status != REQUESTED:
            return refund
        store = self.directory.store(refund.store_code)
        if refund.terminal_refund_id:  # the customer has the money back: the credit note must follow
            return self._complete(refund, store)
        if refund.method == "cash":  # no cash-out was recorded, so none was handed over by the system's account
            refund.fail("interrupted before the cash was paid out; start the refund again")
            self.repo.save_refund(refund)
            return refund
        charge = self.terminal.get_charge(self.repo.get_bill(refund.bill_id).charge_id or "")  # type: ignore[union-attr]
        refund.last_error = "terminal refund state unknown (charge %s shows %d paise refunded); check before retrying" % (charge.charge_id, charge.refunded)
        self.repo.save_refund(refund)
        return refund

    def pending(self) -> List[Refund]:
        return self.repo.refunds(statuses=[MONEY_RETURNED, REQUESTED])


def _rs(paise: int) -> str:
    return "%d.%02d" % (paise // 100, paise % 100)
