"""New customers at the counter, and prescriptions recorded at the till.

The customer goes into Shopify (the system of record for customers); the prescription goes into the store tool
(where prescriptions live). The till checks a prescription before saving it, and records who entered it.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from ..config import BusinessConfig
from ..domain.errors import InvalidRequest, PermissionDenied, StateConflict
from ..domain.optics import to_cd
from ..domain.parties import Customer, normalize_phone
from ..domain.prescription import RX_SOURCES, EyeRx, Prescription, add_months, check_new_prescription
from ..ports import CommercePlatform, PrescriptionSource
from .billing import BillingService
from .common import Audit, Clock, Directory


class CustomerService:
    def __init__(self, commerce: CommercePlatform, prescriptions: PrescriptionSource, config: BusinessConfig,
                 directory: Directory, clock: Clock, audit: Audit, billing: BillingService):
        self.commerce, self.prescriptions, self.config = commerce, prescriptions, config
        self.directory, self.clock, self.audit, self.billing = directory, clock, audit, billing

    def register(self, store_code: str, staff_id: str, first_name: str, last_name: str, phone: str, email: Optional[str] = None) -> Customer:
        store = self.directory.store(store_code)
        self.directory.staff(store, staff_id)
        p = normalize_phone(phone)
        first_name, last_name = (first_name or "").strip(), (last_name or "").strip()
        if not first_name:
            raise InvalidRequest("The customer's first name is needed.", code="missing_field")
        if email and "@" not in email:
            raise InvalidRequest("%r is not an email address." % email, code="bad_email")
        if self.commerce.find_customer_by_phone(p):
            raise StateConflict("%s is already a customer. Look them up instead." % p, code="customer_exists")
        c = self.commerce.create_customer(first_name, last_name, p, (email or "").strip() or None,
                                          "Added at the %s till by %s on %s" % (store.store_code, staff_id, self.clock.today()))
        self.audit(staff_id, "customer.created", str(c.customer_id), store.store_code, phone=p, name=c.name)
        return c

    def add_prescription(self, store_code: str, staff_id: str, phone: str, source: str, issued_on: Optional[str],
                         od: Dict[str, Any], os: Dict[str, Any], pd: Any = None, notes: Optional[str] = None,
                         optometrist_id: Optional[str] = None) -> Prescription:
        store = self.directory.store(store_code)
        staff = self.directory.staff(store, staff_id)
        if source not in RX_SOURCES:
            raise InvalidRequest("Source must be in_store (eye test here) or external (another optometrist's prescription).", code="bad_source")
        if source == "in_store":
            # Only an optometrist can sign off an eye test; a salesperson types it in on their behalf.
            optometrist_id = optometrist_id or (staff.staff_id if staff.role == "optometrist" else None)
            if not optometrist_id:
                raise PermissionDenied("An eye test here must be recorded under the optometrist who did it.", code="optometrist_required")
            self.directory.staff(store, optometrist_id, role="optometrist")
        elif not (notes or "").strip():
            raise InvalidRequest("For a prescription from another optometrist, note who wrote it (doctor or clinic).", code="missing_field")
        customer = self.billing.customer(phone, fresh=True)
        today = self.clock.today()
        try:
            day = date.fromisoformat(issued_on) if issued_on else today
            right, left = _eye(od, "Right eye (OD)"), _eye(os, "Left eye (OS)")
            pd_mm = float(pd) if pd not in (None, "") else None
        except ValueError as e:
            raise InvalidRequest(str(e), code="bad_prescription")
        months = int(self.config.setting("rx_validity_months", 12))  # type: ignore[arg-type]
        valid_until = add_months(day, months)
        problems = check_new_prescription(right, left, day, valid_until, today, pd_mm)
        if problems:
            raise InvalidRequest("The prescription has %d problem(s): %s" % (len(problems), " ".join(problems)), code="bad_prescription",
                                 details={"problems": problems})
        rx = self.prescriptions.add_prescription(Prescription(
            rx_id="", customer_id=customer.customer_id, phone=customer.phone, issued_on=day, valid_until=valid_until, source=source,
            od=right, os=left, pd=pd_mm, optometrist_id=optometrist_id if source == "in_store" else None,
            notes=(notes or "").strip() or None), recorded_by=staff_id)
        self.audit(staff_id, "prescription.recorded", rx.rx_id, store.store_code, phone=customer.phone, source=source,
                   optometrist=rx.optometrist_id, issued_on=day.isoformat())
        return rx


def _eye(e: Dict[str, Any], name: str) -> EyeRx:
    if not isinstance(e, dict) or e.get("sph") in (None, ""):
        raise ValueError("%s: SPH is needed (0.00 if plano)." % name)
    cyl = to_cd(e.get("cyl") or 0) or 0
    axis = e.get("axis")
    axis = int(axis) if axis not in (None, "") else None
    add = e.get("add")
    return EyeRx(sph=to_cd(e["sph"]) or 0, cyl=cyl, axis=axis if cyl else None, add=to_cd(add) if add not in (None, "", 0, "0") else None)
