"""Shared application plumbing: clock, ids, store/staff directory, audit."""
from __future__ import annotations

import secrets
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from ..adapters.sqlite_repo import SqliteRepository
from ..domain.dayend import IST
from ..domain.errors import InvalidRequest, NotFound, PermissionDenied
from ..domain.parties import Staff, Store
from ..ports import CommercePlatform, StaffDirectory


class Clock:
    def now(self) -> datetime:
        return datetime.now(IST).replace(microsecond=0)

    def today(self) -> date:
        return self.now().date()

    def iso(self) -> str:
        return self.now().isoformat()


class FixedClock(Clock):
    def __init__(self, at: datetime):
        self.at = at

    def now(self) -> datetime:
        return self.at


class PinnedDateClock(Clock):
    """Real time of day on a fixed calendar date. Demo/test aid (paired with the mock's --today);
    production always runs on the real clock."""

    def __init__(self, day: date):
        self.day = day

    def now(self) -> datetime:
        return super().now().replace(year=self.day.year, month=self.day.month, day=self.day.day)


def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, secrets.token_hex(6))


class Directory:
    """Stores come from the commerce platform, staff from the store tool."""

    def __init__(self, commerce: CommercePlatform, staff_dir: StaffDirectory):
        self.commerce = commerce
        self.staff_dir = staff_dir

    def stores(self) -> List[Store]:
        return self.commerce.stores()

    def store(self, store_code: str) -> Store:
        for s in self.commerce.stores():
            if s.store_code == store_code:
                if not s.is_billing_store:
                    raise InvalidRequest("%s is not a billing store." % store_code, code="not_a_store")
                return s
        raise NotFound("Unknown store %r. Stores: %s." % (store_code, ", ".join(s.store_code for s in self.commerce.stores() if s.is_billing_store)),
                       code="store_not_found")

    def stores_by_location(self) -> Dict[int, Store]:
        return {s.location_id: s for s in self.commerce.stores()}

    def staff(self, store: Store, staff_id: str, role: Optional[str] = None) -> Staff:
        for s in self.staff_dir.staff(store.store_code):
            if s.staff_id == staff_id:
                if role and s.role != role:
                    raise PermissionDenied("%s is %s; this needs a %s." % (staff_id, s.role, role.replace("_", " ")), code="wrong_role")
                return s
        raise PermissionDenied("%s is not on the staff list for %s." % (staff_id, store.store_code), code="staff_not_in_store")


class Audit:
    def __init__(self, repo: SqliteRepository, clock: Clock):
        self.repo, self.clock = repo, clock

    def __call__(self, actor: str, action: str, ref: Optional[str] = None, store_code: Optional[str] = None,
                 reason_code: Optional[str] = None, /, **details: Any) -> None:
        self.repo.audit(self.clock.iso(), actor or "system", action, ref, store_code, reason_code, details)
