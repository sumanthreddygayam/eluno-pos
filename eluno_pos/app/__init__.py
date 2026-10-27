"""Composition root: the one place that picks concrete adapters for the ports."""
from __future__ import annotations

import time
from datetime import date
from typing import Callable, List, Optional

from ..adapters.http import HttpClient, RetryPolicy, Transport
from ..adapters.shopify import ShopifyAdmin
from ..adapters.smt import StoreTool
from ..adapters.sqlite_repo import SqliteRepository
from ..adapters.terminal import Terminal
from ..config import BusinessConfig, Settings
from ..domain.bill import PAID, PAYMENT_PENDING
from .billing import BillingService
from .common import Audit, Clock, Directory, PinnedDateClock
from .customers import CustomerService
from .refunds import RefundService
from .remakes import RemakeService
from .reports import ReportService
from .tills import TillService


class PosApp:
    def __init__(self, settings: Optional[Settings] = None, clock: Optional[Clock] = None, transport: Optional[Transport] = None,
                 sleep: Callable[[float], None] = time.sleep, policy: RetryPolicy = RetryPolicy()):
        self.settings = settings or Settings.from_env()
        self.clock = clock or (PinnedDateClock(date.fromisoformat(self.settings.business_date)) if self.settings.business_date else Clock())
        self.config = BusinessConfig.load(self.settings.config_dir)

        def client(service: str) -> HttpClient:
            return HttpClient(self.settings.base_url, service, transport=transport, policy=policy, timeout=self.settings.http_timeout, sleep=sleep)

        self.http = {name: client(name) for name in ("shopify", "smt", "terminal")}
        self.commerce = ShopifyAdmin(self.http["shopify"])
        self.store_tool = StoreTool(self.http["smt"])
        self.terminal = Terminal(self.http["terminal"])
        self.repo = SqliteRepository(self.settings.db_path)
        self.audit = Audit(self.repo, self.clock)
        self.directory = Directory(self.commerce, self.store_tool)

        self.billing = BillingService(self.commerce, self.store_tool, self.terminal, self.repo, self.config, self.directory, self.clock, self.audit)
        self.refunds = RefundService(self.commerce, self.terminal, self.repo, self.config, self.directory, self.clock, self.audit, self.billing)
        self.remakes = RemakeService(self.commerce, self.store_tool, self.repo, self.config, self.directory, self.clock, self.audit,
                                     self.billing, self.refunds)
        self.customers = CustomerService(self.commerce, self.store_tool, self.config, self.directory, self.clock, self.audit, self.billing)
        self.tills = TillService(self.repo, self.config, self.directory, self.clock, self.audit)
        self.reports = ReportService(self.commerce, self.repo, self.config, self.directory)

    def sync_pending(self) -> List[str]:
        """Push everything paid but not yet in Shopify (run on start-up, on a timer, or by hand)."""
        out = []
        for b in self.billing.pending():
            if b.status == PAYMENT_PENDING:
                out.append("%s: payment pending; resume with `pay %s`" % (b.bill_id, b.bill_id))
                continue
            b = self.billing.sync(b)
            out.append("%s %s: %s%s" % (b.invoice_no, b.bill_id, b.status, (" (" + b.last_error + ")") if b.status == PAID and b.last_error else ""))
        for r in self.refunds.pending():
            r = self.refunds.resume(r)
            out.append("%s %s: %s%s" % (r.credit_note_no or "-", r.refund_id, r.status, (" (" + r.last_error + ")") if r.last_error else ""))
        return out

    def close(self) -> None:
        self.repo.close()
