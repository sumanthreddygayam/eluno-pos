"""JSON HTTP API for a till front-end. Money in requests and responses is integer paise.

  GET  /health
  GET  /customers/{phone}                       customer, flags, latest valid Rx, skipped Rx
  POST /quotes                                  price a cart (BillRequest JSON); nothing saved
  POST /bills                                   create a draft bill; add "pay": "upi|card|cash" to pay at once
  GET  /bills/{bill_id}
  POST /bills/{bill_id}/pay      {"method"}     safe to repeat (Wi-Fi drop): never charges or creates twice
  POST /bills/{bill_id}/cancel   {"staff_id", "why", "approver_id"?}   (manager needed if stuck at the terminal)
  POST /bills/{bill_id}/void     {"staff_id", "approver_id", "reason_code"}
  POST /sync                                    push pending bills/refunds to Shopify
  POST /refunds  {"invoice_no", "store_code", "staff_id", "reason_code", "lines"?, "approver_id"?}
  POST /remakes  {"parent_invoice_no", "store_code", "staff_id", "reason_code", "cost_bearer", "pair_ref"?, "lens"?}
  GET  /invoices/{invoice_no}.txt|.html|.json   (URL-encode the slashes: ELU%2FHSR%2FFY26%2F00003)
  GET  /tills/{store}
  POST /tills/{store}/open|paid-in|paid-out|close  {"staff_id", "amount_paise", "reason_code"?, "note"?}
  GET  /reports/day-end?store=HSR&date=2026-10-22
  GET  /reports/channels?date=2026-10-22        bills by channel, by store
  GET  /credit-notes/{credit_note_no}.txt         (URL-encode the slashes)
  GET  /audit?ref=&store=
"""
from __future__ import annotations

import json
import logging
import re
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from ..app import PosApp
from ..app.billing import BillRequest
from ..config import Settings
from ..domain.errors import DomainError, InvalidRequest, NotFound
from ..invoice_render import render_credit_note_text, render_html, render_text
from ..ports import IntegrationError

log = logging.getLogger("eluno_pos.api")


class Api:
    def __init__(self, app: PosApp):
        self.app = app
        # Requests run in parallel (ThreadingHTTPServer): a customer approving UPI at one store must not make
        # another store wait. The database serialises its own transactions; BillingService locks per bill.
        self.routes: List[Tuple[str, "re.Pattern[str]", Callable]] = []
        r = self.routes
        for method, pattern, fn in [
            ("GET", r"/health", self.health),
            ("GET", r"/customers/(?P<phone>[^/]+)", self.customer),
            ("POST", r"/quotes", self.quote),
            ("POST", r"/bills", self.create_bill),
            ("GET", r"/bills/(?P<bill_id>[\w-]+)", self.get_bill),
            ("POST", r"/bills/(?P<bill_id>[\w-]+)/pay", self.pay),
            ("POST", r"/bills/(?P<bill_id>[\w-]+)/cancel", self.cancel),
            ("POST", r"/bills/(?P<bill_id>[\w-]+)/void", self.void),
            ("POST", r"/sync", self.sync),
            ("POST", r"/refunds", self.refund),
            ("POST", r"/remakes", self.remake),
            ("GET", r"/invoices/(?P<invoice>[^/]+)\.(?P<fmt>txt|html|json)", self.invoice),
            ("GET", r"/tills/(?P<store>\w+)", self.till),
            ("POST", r"/tills/(?P<store>\w+)/(?P<action>open|paid-in|paid-out|close)", self.till_action),
            ("GET", r"/reports/day-end", self.day_end),
            ("GET", r"/reports/channels", self.channels),
            ("GET", r"/credit-notes/(?P<cn>[^/]+)\.txt", self.credit_note),
            ("GET", r"/audit", self.audit),
        ]:
            r.append((method, re.compile("^%s$" % pattern), fn))

    def dispatch(self, method: str, raw_path: str, body: Optional[dict]) -> Tuple[int, Any, str]:
        u = urlparse(raw_path)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        for m, rx, fn in self.routes:
            match = rx.match(u.path)
            if match and m == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                return fn(body or {}, query, **params)
        raise NotFound("no route %s %s" % (method, u.path))

    # ------------------------------------------------------------ handlers
    def health(self, body, q):
        return 200, {"ok": True}, "json"

    def customer(self, body, q, phone):
        return 200, self.app.billing.lookup(phone), "json"

    def quote(self, body, q):
        return 200, self.app.billing.quote(_bill_request(body)).to_dict(), "json"

    def create_bill(self, body, q):
        bill, quote = self.app.billing.create(_bill_request(body))
        if body.get("pay"):
            bill = self.app.billing.pay(bill.bill_id, body["pay"], body.get("staff_id"))
        return 201, {"bill": bill.to_dict(), "quote": quote.to_dict()}, "json"

    def get_bill(self, body, q, bill_id):
        return 200, self.app.billing.get(bill_id).to_dict(), "json"

    def pay(self, body, q, bill_id):
        return 200, self.app.billing.pay(bill_id, _req(body, "method"), body.get("staff_id")).to_dict(), "json"

    def cancel(self, body, q, bill_id):
        return 200, self.app.billing.cancel(bill_id, _req(body, "staff_id"), body.get("why", "customer did not proceed"),
                                            body.get("approver_id")).to_dict(), "json"

    def void(self, body, q, bill_id):
        return 200, self.app.billing.void(bill_id, _req(body, "staff_id"), _req(body, "approver_id"), _req(body, "reason_code")).to_dict(), "json"

    def sync(self, body, q):
        return 200, {"results": self.app.sync_pending()}, "json"

    def refund(self, body, q):
        r = self.app.refunds.refund(_req(body, "invoice_no"), _req(body, "store_code"), _req(body, "staff_id"), _req(body, "reason_code"),
                                    body.get("lines"), body.get("approver_id"), body.get("note"))
        return 201, r.to_dict(), "json"

    def remake(self, body, q):
        b = self.app.remakes.remake(_req(body, "parent_invoice_no"), _req(body, "store_code"), _req(body, "staff_id"), _req(body, "reason_code"),
                                    _req(body, "cost_bearer"), int(body.get("pair_ref", 1)), body.get("lens"), tuple(body.get("acknowledge") or ()))
        return 201, b.to_dict(), "json"

    def invoice(self, body, q, invoice, fmt):
        bill = self.app.repo.get_bill_by_invoice(invoice)
        if bill is None:
            if not q.get("store"):
                raise NotFound("Invoice %s is not in this POS; add ?store= to read it from Shopify." % invoice)
            bill = self.app.refunds.find_bill(invoice, self.app.directory.store(q["store"]))
        store = self.app.directory.store(bill.store_code)
        if fmt == "json":
            return 200, bill.to_dict(), "json"
        return 200, render_html(bill, store) if fmt == "html" else render_text(bill, store), fmt

    def till(self, body, q, store):
        return 200, self.app.tills.current(store).to_dict(), "json"

    def till_action(self, body, q, store, action):
        t = self.app.tills
        staff = _req(body, "staff_id")
        if action == "open":
            s = t.open(store, staff, int(body.get("amount_paise", 0)))
        elif action == "close":
            s = t.close(store, staff, int(_req(body, "amount_paise")), body.get("note"))
        else:
            s = t.move(store, staff, action.replace("-", "_"), int(_req(body, "amount_paise")), _req(body, "reason_code"), body.get("note"))
        return 200, s.to_dict(), "json"

    def day_end(self, body, q):
        day = date.fromisoformat(q["date"]) if q.get("date") else self.app.clock.today()
        return 200, self.app.reports.day_end(_req(q, "store"), day).to_dict(), "json"

    def channels(self, body, q):
        return 200, self.app.reports.channels(date.fromisoformat(q["date"]) if q.get("date") else self.app.clock.today()), "json"

    def credit_note(self, body, q, cn):
        r = self.app.repo.get_refund_by_credit_note(cn)
        if r is None:
            raise NotFound("No credit note %s." % cn)
        bill = self.app.repo.get_bill(r.bill_id)
        return 200, render_credit_note_text(r, bill, self.app.directory.store(bill.store_code)), "txt"

    def audit(self, body, q):
        return 200, {"events": self.app.repo.audit_log(ref=q.get("ref"), store_code=q.get("store"), limit=int(q.get("limit", 100)))}, "json"


def _req(d: Dict[str, Any], key: str) -> Any:
    if d.get(key) in (None, ""):
        raise InvalidRequest("%s is required" % key, code="missing_field")
    return d[key]


def _bill_request(body: dict) -> BillRequest:
    try:
        return BillRequest.from_dict(body)
    except (KeyError, TypeError) as e:
        raise InvalidRequest("bad bill request: %s" % e, code="bad_request")


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "eluno-pos/1.0"

        def _respond(self, status: int, payload: Any, fmt: str = "json") -> None:
            if fmt == "json":
                data, ctype = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
            else:
                data, ctype = payload.encode("utf-8"), "text/html; charset=utf-8" if fmt == "html" else "text/plain; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._respond(400, {"error": {"code": "bad_json", "message": "body is not valid JSON"}})
            try:
                status, payload, fmt = api.dispatch(method, self.path, body)
                self._respond(status, payload, fmt)
            except DomainError as e:
                self._respond(e.http_status, {"error": e.to_dict()})
            except IntegrationError as e:
                self._respond(502, {"error": {"code": "upstream_error", "message": str(e), "retryable": e.retryable}})
            except Exception as e:  # pragma: no cover - last resort, keep the till informed
                log.error("unhandled error: %s\n%s", e, traceback.format_exc())
                self._respond(500, {"error": {"code": "internal_error", "message": str(e)}})

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.command, self.path)

    return Handler


def serve(settings: Settings, port: int = 8000) -> None:
    app = PosApp(settings)
    print("Eluno POS API on http://127.0.0.1:%d  (backend %s, db %s)" % (port, settings.base_url, settings.db_path))
    try:
        for series, info in app.billing.cutover().items():
            print("  series %-22s last issued %05d" % (series, info["last"]))
    except Exception as e:  # Shopify down at start-up: billing still refuses safely per store until it is back
        print("  cutover check failed (%s); stores without an initialised series will refuse payment until it succeeds" % e)
    for line in app.sync_pending():
        print("  startup sync: " + line)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(Api(app)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
