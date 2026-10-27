"""Day-end report: numbers from Shopify, checked against the POS ledger and the till."""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Dict, List

from ..adapters.sqlite_repo import SqliteRepository
from ..config import BusinessConfig
from ..domain.bill import PAID, PAYMENT_PENDING
from ..domain.dayend import DayEndReport, Finding, build_day_end, reconcile
from ..domain.documents import CREDIT_NOTE, INVOICE, series_key
from ..domain.money import format_inr
from ..ports import CommercePlatform, IntegrationError
from .common import Directory

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def _topic(message: str) -> str:
    """Which part of the day an alert is about, so the screen can group and filter them."""
    m = message.lower()
    if "reserved" in m or "card machine" in m:
        return "payments"
    if re.search(r"\b(cash|till|drawer)\b", m):
        return "cash"
    if "series" in m or "invoice number" in m or "no credit note" in m:
        return "documents"
    if "shopify" in m:
        return "shopify"
    return "other"


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
                rep.findings.append(Finding("error", "Cash in Shopify (net Rs %s) does not match the till's cash sales less refunds (Rs %s)." % (format_inr(cash_net.net), format_inr(till_sales))))
            if s.variance_flagged:
                rep.findings.append(Finding("warning", "Till variance Rs %s exceeds the alert threshold; flagged to finance." % format_inr(s.variance_paise or 0)))
        elif rep.by_payment_method.get("cash"):
            rep.findings.append(Finding("warning", "Cash bills in Shopify but no till session recorded for %s on %s." % (store_code, day)))
        return rep

    def all_stores(self, day: date) -> Dict[str, Any]:
        """Every store's day-end side by side, totals, and one list of everything that needs a look."""
        rows: List[Dict[str, Any]] = []
        alerts: List[Dict[str, Any]] = []
        totals = {k: 0 for k in ("bills", "net_sales", "refunds_count", "refunds_amount", "net_after_refunds", "discounts", "tax",
                                 "cash_expected", "cash_variance", "pending_sync", "pending_sync_amount", "waiting_on_terminal")}
        totals["by_payment_method"] = {}

        def alert(store, severity: str, message: str) -> None:
            alerts.append({"store_code": store.store_code, "store_name": store.name, "severity": severity, "topic": _topic(message), "message": message})

        for store in self.directory.stores():
            if not store.is_billing_store:
                continue
            try:
                rep = self.day_end(store.store_code, day)
            except IntegrationError as e:
                rows.append({"store_code": store.store_code, "store_name": store.name, "error": str(e)})
                alert(store, "error", "Couldn't read this store's day from Shopify: %s" % e)
                continue
            local = self.repo.bills(store_code=store.store_code, business_date=day.isoformat())
            pending_sync = [b for b in local if b.status == PAID]
            waiting = [b for b in local if b.status == PAYMENT_PENDING]
            till = rep.till or {}
            for f in rep.findings:
                alert(store, f.severity, f.message)
            if waiting:
                alert(store, "warning", "%d bill(s) waiting on the card machine, which never confirmed: %s. Retry the payment, or a manager "
                                        "cancels after checking the terminal." % (len(waiting), ", ".join(b.bill_id for b in waiting)))
            variance = till.get("variance")
            if variance and not till.get("flagged"):
                alert(store, "info", "Cash drawer %s by Rs %s at close (within the alert limit)." % ("short" if variance < 0 else "over", format_inr(abs(variance))))
            if till.get("status") == "open":
                alert(store, "info", "Cash drawer still open: Rs %s expected in it. Count and close it at the end of the day." % format_inr(till.get("expected") or 0))
            methods = {k: v.net for k, v in rep.by_payment_method.items()}
            row = {
                "store_code": store.store_code, "store_name": store.name, "bills": rep.bills, "net_sales": rep.net_sales,
                "refunds_count": rep.refunds_count, "refunds_amount": rep.refunds_amount, "net_after_refunds": rep.net_after_refunds,
                "discounts": rep.discounts, "tax": rep.tax, "average_bill": rep.average_bill, "by_payment_method": methods,
                "till": till or None, "pending_sync": len(pending_sync), "pending_sync_amount": sum(b.total for b in pending_sync),
                "waiting_on_terminal": len(waiting),
                "alerts": {s: sum(1 for a in alerts if a["store_code"] == store.store_code and a["severity"] == s) for s in SEVERITY_ORDER},
            }
            rows.append(row)
            for k in ("bills", "net_sales", "refunds_count", "refunds_amount", "net_after_refunds", "discounts", "tax", "pending_sync", "pending_sync_amount",
                      "waiting_on_terminal"):
                totals[k] += row[k]
            totals["cash_expected"] += till.get("expected") or 0
            totals["cash_variance"] += till.get("variance") or 0
            for k, v in methods.items():
                totals["by_payment_method"][k] = totals["by_payment_method"].get(k, 0) + v
        alerts.sort(key=lambda a: (SEVERITY_ORDER.get(a["severity"], 9), a["store_code"]))
        return {"date": day.isoformat(), "currency": "INR", "unit": "paise", "stores": rows, "totals": totals, "alerts": alerts,
                "alert_counts": {s: sum(1 for a in alerts if a["severity"] == s) for s in SEVERITY_ORDER}}
