"""Store tool (SMT) adapter: prescriptions by phone, staff by store."""
from __future__ import annotations

import secrets
import time
from datetime import date
from typing import Dict, List, Tuple

from ..domain.optics import cd_to_float, to_cd
from ..domain.parties import Staff
from ..domain.prescription import EyeRx, Prescription
from ..ports import AmbiguousWrite, IntegrationError
from .http import HttpClient


def prescription_from(r: dict) -> Prescription:
    def eye(e: dict) -> EyeRx:
        return EyeRx(sph=to_cd(e["sph"]), cyl=to_cd(e.get("cyl") or 0), axis=e.get("axis"), add=to_cd(e.get("add")))
    pd = (r.get("pd") or {}).get("binocular")
    return Prescription(rx_id=r["rx_id"], customer_id=int(r["customer_id"]), phone=r["phone"], issued_on=date.fromisoformat(r["date"]),
                        valid_until=date.fromisoformat(r["valid_until"]), source=r.get("source", "in_store"), od=eye(r["od"]), os=eye(r["os"]),
                        pd=pd, optometrist_id=r.get("optometrist_id"), notes=r.get("notes"))


def prescription_payload(rx: Prescription, recorded_by: str, client_ref: str) -> dict:
    def eye(e: EyeRx) -> dict:
        return {"sph": cd_to_float(e.sph), "cyl": cd_to_float(e.cyl), "axis": e.axis, "add": cd_to_float(e.add)}
    return {"customer_id": rx.customer_id, "phone": rx.phone, "date": rx.issued_on.isoformat(), "valid_until": rx.valid_until.isoformat(),
            "source": rx.source, "optometrist_id": rx.optometrist_id, "od": eye(rx.od), "os": eye(rx.os),
            "pd": {"binocular": rx.pd, "mono_r": None, "mono_l": None}, "notes": rx.notes, "recorded_by": recorded_by, "client_ref": client_ref}


def staff_from(s: dict) -> Staff:
    return Staff(staff_id=s["staff_id"], name=s.get("name", ""), role=s["role"], store_code=s["store_code"],
                 badges=tuple(s.get("badges") or ()), can_override_discount=bool(s.get("can_override_discount")))


class StoreTool:
    def __init__(self, client: HttpClient, staff_ttl: float = 300.0):
        self.client = client
        self.staff_ttl = staff_ttl
        self._staff: Dict[str, Tuple[float, List[Staff]]] = {}

    def prescriptions(self, phone: str) -> List[Prescription]:
        return [prescription_from(r) for r in (self.client.get("smt/prescriptions", {"phone": phone}) or {}).get("prescriptions", [])]

    def add_prescription(self, rx: Prescription, recorded_by: str) -> Prescription:
        """Record a new prescription. A lost answer may mean it was saved, so look for our reference before sending again."""
        ref = "till_" + secrets.token_hex(6)
        payload = prescription_payload(rx, recorded_by, ref)
        last = None
        for _ in range(3):
            try:
                return prescription_from(self.client.post("smt/prescriptions", payload, idempotent=False)["prescription"])
            except AmbiguousWrite as e:
                last = e
                saved = [r for r in (self.client.get("smt/prescriptions", {"phone": rx.phone}) or {}).get("prescriptions", []) if r.get("client_ref") == ref]
                if saved:
                    return prescription_from(saved[0])
        raise IntegrationError("smt", "could not record the prescription for %s: %s" % (rx.phone, last))

    def staff(self, store_code: str) -> List[Staff]:
        hit = self._staff.get(store_code)
        if hit is None or time.monotonic() - hit[0] > self.staff_ttl:
            hit = (time.monotonic(), [staff_from(s) for s in (self.client.get("smt/staff", {"store": store_code}) or {}).get("staff", [])])
            self._staff[store_code] = hit
        return hit[1]
