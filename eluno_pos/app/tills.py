"""Cash till: open with a float, paid-ins/outs with reasons, close with a count, flag variance."""
from __future__ import annotations

import logging
from typing import Optional

from ..adapters.sqlite_repo import SqliteRepository
from ..config import BusinessConfig
from ..domain.errors import InvalidRequest, NotFound
from ..domain.till import TillSession
from .common import Audit, Clock, Directory

log = logging.getLogger("eluno_pos.till")


class TillService:
    def __init__(self, repo: SqliteRepository, config: BusinessConfig, directory: Directory, clock: Clock, audit: Audit):
        self.repo, self.config, self.directory, self.clock, self.audit = repo, config, directory, clock, audit

    def current(self, store_code: str) -> TillSession:
        s = self.repo.open_till(store_code)
        if s is None:
            raise NotFound("No open till at %s." % store_code, code="till_not_open")
        return s

    def open(self, store_code: str, staff_id: str, float_paise: int) -> TillSession:
        store = self.directory.store(store_code)
        self.directory.staff(store, staff_id)
        if not isinstance(float_paise, int) or float_paise < 0:
            raise InvalidRequest("Opening float must be zero or more paise.", code="bad_amount")
        day = self.clock.today().isoformat()
        sid, n = "TILL-%s-%s" % (store_code, day), 1
        while self.repo.till_exists(sid):
            n += 1
            sid = "TILL-%s-%s-%d" % (store_code, day, n)
        s = TillSession(session_id=sid, store_code=store_code, location_id=store.location_id, opened_at=self.clock.iso(),
                        opened_by=staff_id, opening_float_paise=float_paise)
        self.repo.save_till(s)
        self.audit(staff_id, "till.opened", sid, store_code, float_paise=float_paise)
        return s

    def move(self, store_code: str, staff_id: str, kind: str, amount: int, reason_code: str, note: Optional[str] = None) -> TillSession:
        if kind not in ("paid_in", "paid_out"):
            raise InvalidRequest("Use paid_in or paid_out; sales and refunds are recorded by billing.", code="bad_movement")
        store = self.directory.store(store_code)
        self.directory.staff(store, staff_id)
        self.config.require_reason(kind, reason_code)
        if reason_code == "OTHER" and not note:
            raise InvalidRequest("Reason OTHER needs a note.", code="note_required")
        with self.repo.transaction():
            s = self.current(store_code)
            prefix = "PO" if kind == "paid_out" else "PI"
            series = "%s-%s-" % (prefix, store_code)
            if not self.repo.series_exists(series):
                self.repo.init_series(series, 0, self.clock.iso())
            ref = self.repo.allocate(series, kind, store_code, s.session_id, self.clock.iso())
            s.record(kind, amount, ref, self.clock.iso(), staff_id, reason_code, note)
            self.repo.save_till(s)
            self.audit(staff_id, "till.%s" % kind, s.session_id, store_code, reason_code, amount=amount, ref=ref, note=note)
        return s

    def close(self, store_code: str, staff_id: str, counted_paise: int, note: Optional[str] = None) -> TillSession:
        store = self.directory.store(store_code)
        self.directory.staff(store, staff_id)
        threshold = int(self.config.setting("till_variance_alert_paise", 20000))  # type: ignore[arg-type]
        with self.repo.transaction():
            s = self.current(store_code)
            s.close(counted_paise, self.clock.iso(), staff_id, threshold, note)
            self.repo.save_till(s)
            self.audit(staff_id, "till.closed", s.session_id, store_code, expected=s.expected_cash_paise, counted=counted_paise,
                       variance=s.variance_paise, flagged=s.variance_flagged)
            if s.variance_flagged:
                # v1: recorded as an alert in the audit trail and printed; finance notification (email/WhatsApp) is a follow-up
                self.audit("system", "till.variance_alert", s.session_id, store_code, variance=s.variance_paise, threshold=threshold,
                           notify="finance")
                log.warning("Till %s variance %d paise exceeds %d; finance to be notified", s.session_id, s.variance_paise, threshold)
        return s
