"""Payment terminal adapter (Razorpay-like).

Charges are idempotent on order_ref by contract (the bill id), so they are retried on 503 and on
lost responses. Refunds carry no idempotency key, so after an ambiguous refund call we read the
charge back and compare refunded amounts before deciding whether to try again.

Note on the mock: it only honours order_ref for the first 503; a second POST after a *successful*
charge creates a second charge. The billing service therefore never charges a bill that already has
a charge id, and the charge id is persisted the moment it is known.
"""
from __future__ import annotations

from ..ports import AmbiguousWrite, Charge, IntegrationError, TerminalRefund
from .http import HttpClient


def _charge(d: dict) -> Charge:
    return Charge(charge_id=d["charge_id"], status=d.get("status", ""), amount=int(d.get("amount_paise", 0)),
                  method=d.get("method", ""), refunded=int(d.get("refunded_paise", 0)))


class Terminal:
    def __init__(self, client: HttpClient):
        self.client = client

    def charge(self, amount: int, method: str, order_ref: str) -> Charge:
        c = _charge(self.client.post("terminal/charges", {"amount_paise": amount, "method": method, "order_ref": order_ref}, idempotent=True))
        if c.status != "captured" or c.amount != amount:
            raise IntegrationError("terminal", "charge %s is %s for %d paise (expected captured %d)" % (c.charge_id, c.status, c.amount, amount), retryable=False)
        return c

    def get_charge(self, charge_id: str) -> Charge:
        return _charge(self.client.get("terminal/charges/%s" % charge_id))

    def refund(self, charge_id: str, amount: int, reason: str) -> TerminalRefund:
        before = self.get_charge(charge_id).refunded
        try:
            d = self.client.post("terminal/refunds", {"charge_id": charge_id, "amount_paise": amount, "reason": reason}, idempotent=False)
            return TerminalRefund(refund_id=d["refund_id"], status=d.get("status", ""), amount=int(d.get("amount_paise", amount)))
        except AmbiguousWrite:
            after = self.get_charge(charge_id).refunded
            if after - before == amount:
                return TerminalRefund(refund_id="recovered:%s:%d" % (charge_id, after), status="processed", amount=amount, recovered=True)
            if after == before:
                d = self.client.post("terminal/refunds", {"charge_id": charge_id, "amount_paise": amount, "reason": reason}, idempotent=False)
                return TerminalRefund(refund_id=d["refund_id"], status=d.get("status", ""), amount=int(d.get("amount_paise", amount)))
            raise
