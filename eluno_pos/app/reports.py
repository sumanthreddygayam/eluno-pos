"""Day-end report: numbers from Shopify, checked against the POS ledger and the till."""
from __future__ import annotations

from datetime import date

from ..adapters.sqlite_repo import SqliteRepository
from ..config import BusinessConfig
from ..domain.dayend import DayEndReport, Finding, build_day_end, reconcile
from ..domain.documents import CREDIT_NOTE, INVOICE, series_key
from ..ports import CommercePlatform
from .common import Directory


class ReportService:
    def __init__(self, commerce: CommercePlatform, repo: SqliteRepository, config: BusinessConfig, directory: Directory):
        self.commerce, self.repo, self.config, self.directory = commerce, repo, config, directory

    def channels(self, day: date) -> dict:
        """Where bills came from (walk-in / WhatsApp-booked / online pickup), by store, for one day."""
        lookback = int(self.config.setting("refund_lookback_days", 90))  # type: ignore[arg-type]
        out = {}
        for store in self.directory.stores():
            if not store.is_billing_store:
                continue
            rep = build_day_end(store.store_code, day, self.commerce.sales_for_day(store, day, lookback))
            out[store.store_code] = {ch: {"bills": m.bills, "net": m.net} for ch, m in sorted(rep.by_channel.items())}
        return out

    def day_end(self, store_code: str, day: date) -> DayEndReport:
        store = self.directory.store(store_code)
        lookback = int(self.config.setting("refund_lookback_days", 90))  # type: ignore[arg-type]
        records = self.commerce.sales_for_day(store, day, lookback)
        rep = build_day_end(store_code, day, records)
        local_bills = self.repo.bills(store_code=store_code, business_date=day.isoformat(), statuses=["paid", "synced", "voided"])
        local_refunds = self.repo.refunds(store_code=store_code, day=day.isoformat())
        reconcile(rep, [r for r in records if r.invoice_no in set(rep.invoices)], local_bills, local_refunds)

        for kind in (INVOICE, CREDIT_NOTE):
            gaps = self.repo.series_gaps(series_key(store.invoice_prefix or "", kind))
            if gaps:
                rep.findings.append(Finding("error", "%s series has gaps at %s." % (kind.replace("_", " "), gaps)))
        stuck = [h for h in self.repo.hold_list(store.location_id) if h["status"] in ("draft", "payment_pending")]
        if stuck:
            rep.findings.append(Finding("warning", "%d frame(s) still reserved by unfinished bills (payment declined or terminal stuck): %s. "
                                                   "Complete or cancel them so the frames can be sold." % (
                                                       sum(h["qty"] for h in stuck), ", ".join("%s (%s)" % (h["bill_id"], h["status"]) for h in stuck))))
        cancelled = [d["number"] for d in self.repo.documents(store_code, INVOICE) if d["status"] == "cancelled" and d["issued_at"][:10] == day.isoformat()]
        if cancelled:
            rep.findings.append(Finding("info", "Cancelled invoice numbers (kept in the series): %s" % ", ".join(cancelled)))

        tills = self.repo.tills(store_code, day.isoformat())
        if tills:
            s = tills[-1]
            cash_net = rep.by_payment_method.get("cash")
            till_sales = sum(t.total("cash_sale") for t in tills) + sum(t.total("cash_refund") for t in tills)
            rep.till = {"session_id": s.session_id, "status": s.status, "opening_float": s.opening_float_paise,
                        "cash_sales": sum(t.total("cash_sale") for t in tills), "cash_refunds": -sum(t.total("cash_refund") for t in tills),
                        "paid_in": sum(t.total("paid_in") for t in tills), "paid_out": -sum(t.total("paid_out") for t in tills),
                        "expected": s.expected_cash_paise, "counted": s.counted_cash_paise, "variance": s.variance_paise,
                        "flagged": s.variance_flagged}
            if cash_net is not None and cash_net.net != till_sales:
                rep.findings.append(Finding("error", "Cash in Shopify (net %d paise) does not match the till's cash sales less refunds (%d paise)." % (cash_net.net, till_sales)))
            if s.variance_flagged:
                rep.findings.append(Finding("warning", "Till variance %d paise exceeds the alert threshold; flagged to finance." % (s.variance_paise or 0)))
        elif rep.by_payment_method.get("cash"):
            rep.findings.append(Finding("warning", "Cash bills in Shopify but no till session recorded for %s on %s." % (store_code, day)))
        return rep
