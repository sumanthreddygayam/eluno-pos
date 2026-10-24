"""Remakes: a new lens for an existing pair at no charge (non-adaptation, wrong external Rx, lab defect).

Per Ops and Optometry: the frame is not billed again and frame stock is not touched; the new order
carries only the lens line, references the parent invoice, and records who bears the lab cost.
It still gets an invoice number (a zero-value tax invoice) so the series stays complete.
"""
from __future__ import annotations

from typing import Optional

from ..adapters.sqlite_repo import SqliteRepository
from ..config import BusinessConfig
from ..domain.bill import SYNCED, Bill, BillLine
from ..domain.catalog import LensChoice
from ..domain.errors import BillBlocked, InvalidRequest, StateConflict
from ..domain.issues import blocking
from ..domain.lens_rules import anomaly_flags, check_lens
from ..domain.pricing import price_line
from ..ports import CommercePlatform, PrescriptionSource
from .billing import BillingService
from .common import Audit, Clock, Directory, new_id
from .refunds import RefundService


class RemakeService:
    def __init__(self, commerce: CommercePlatform, prescriptions: PrescriptionSource, repo: SqliteRepository, config: BusinessConfig,
                 directory: Directory, clock: Clock, audit: Audit, billing: BillingService, refunds: RefundService):
        self.commerce, self.prescriptions, self.repo, self.config = commerce, prescriptions, repo, config
        self.directory, self.clock, self.audit, self.billing, self.refunds = directory, clock, audit, billing, refunds

    def remake(self, parent_invoice_no: str, store_code: str, staff_id: str, reason_code: str, cost_bearer: str,
               pair_ref: int = 1, lens: Optional[str] = None, acknowledge=()) -> Bill:
        store = self.directory.store(store_code)
        staff = self.directory.staff(store, staff_id)
        self.config.require_reason("remake", reason_code)
        self.config.require_reason("remake_cost_bearer", cost_bearer)
        parent = self.refunds.find_bill(parent_invoice_no, store)
        if parent.status != SYNCED:
            raise StateConflict("%s has not reached Shopify yet; sync it first." % parent_invoice_no, code="bill_not_synced")
        old = next((l for l in parent.lines if l.pair_ref == pair_ref and l.kind == "lens"), None)
        if old is None:
            raise InvalidRequest("%s has no lens in pair %d." % (parent_invoice_no, pair_ref), code="no_lens_in_pair")
        refunded = {l.line_ref for r in self.repo.refunds(bill_id=parent.bill_id) for l in r.lines}
        if old.line_ref in refunded:
            raise StateConflict("The lens on %s pair %d was refunded; a remake is not possible." % (parent_invoice_no, pair_ref), code="already_refunded")

        catalog = self.commerce.catalog()
        new_lens = catalog.lens(LensChoice.parse(lens)) if lens else catalog.lenses_by_variant.get(old.variant_id)
        if new_lens is None:
            raise InvalidRequest("The original lens is no longer in the catalog; pass lens=type/index/coating.", code="lens_not_found")
        phone = old.wearer_phone or parent.customer_phone
        selection = self.billing.rx_selection(phone)  # the newest valid Rx: a re-test may have replaced the wrong one
        today = self.clock.today()
        issues = check_lens(new_lens, selection, today, catalog, pair_ref) + anomaly_flags(new_lens, selection, staff, pair_ref)
        stop = blocking(issues, acknowledge)
        if stop:
            raise BillBlocked("The remake lens cannot be ordered: %d issue(s)." % len(stop), stop)

        offer_id = str(self.config.setting("remake_offer_id", "REMAKE_GOODWILL"))
        priced = price_line("%dL" % pair_ref, new_lens.mrp, new_lens.hsn, [(offer_id, new_lens.mrp)], self.config.taxes, today)
        rx = selection.chosen
        now = self.clock.iso()
        line = BillLine(line_ref=priced.line_ref, pair_ref=pair_ref, kind="lens", sku=new_lens.sku, variant_id=new_lens.variant_id,
                        title=new_lens.title, variant_title=new_lens.coating, description=new_lens.description(rx.rx_id if rx else None),
                        hsn=new_lens.hsn, rate_bps=priced.tax.rate_bps, mrp=priced.mrp, discounts=[[offer_id, priced.discount]],
                        taxable=priced.tax.taxable, cgst=priced.tax.cgst, sgst=priced.tax.sgst, lens_config=new_lens.config_code,
                        rx_id=rx.rx_id if rx else None, rx_json=rx.line_property() if rx else None,
                        wearer_customer_id=old.wearer_customer_id or parent.customer_id, wearer_name=old.wearer_name or parent.customer_name,
                        wearer_phone=phone)
        bill = Bill(bill_id=new_id("bill"), store_code=store.store_code, location_id=store.location_id, customer_id=parent.customer_id,
                    customer_name=parent.customer_name, customer_phone=parent.customer_phone, staff_id=staff_id, channel="walk_in",
                    business_date=today.isoformat(), lines=[line], kind="remake",
                    offers=[{"offer_id": offer_id, "name": "Remake at no charge", "amount": priced.discount}],
                    notices=[i.to_dict() for i in issues], acknowledged=list(acknowledge), parent_invoice_no=parent_invoice_no,
                    remake_reason=reason_code, remake_cost_bearer=cost_bearer, created_at=now, updated_at=now)
        self.repo.save_bill(bill)
        self.audit(staff_id, "remake.created", bill.bill_id, store.store_code, reason_code, parent_invoice_no=parent_invoice_no,
                   cost_bearer=cost_bearer, lens=new_lens.config_code, rx_id=line.rx_id, lab_cost_paise=new_lens.mrp)
        return self.billing.pay(bill.bill_id, "none", staff_id)
