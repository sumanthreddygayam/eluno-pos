"""JSON HTTP API for a till front-end. Money in requests and responses is integer paise.

  GET  /health
  GET  /customers/{phone}                       customer, flags, latest valid Rx, skipped Rx
  POST /customers  {"store_code", "staff_id", "phone", "first_name", "last_name"?, "email"?}        new customer, in Shopify
  POST /customers/{phone}/prescriptions  {"store_code", "staff_id", "source": "in_store|external", "issued_on"?,
       "od": {"sph","cyl","axis","add"}, "os": {...}, "pd"?, "notes"?, "optometrist_id"?}         checked, then saved in the store tool
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
  GET  /reports/all-stores?date=2026-10-22      every store's day-end, totals, and every alert (cash mismatch, Shopify pending, ...)
  GET  /credit-notes/{credit_note_no}.txt         (URL-encode the slashes)
  GET  /audit?ref=&store=

For the till screen (eluno_pos/web/, served at /):
  GET  /meta                                    business date, channels, payment methods, reason codes
  GET  /stores                                  billing stores
  GET  /staff?store=HSR
  GET  /catalog?store=HSR                       frames with stock here (Shopify count minus frames held by unfinished bills), lens matrix
  GET  /bills?store=HSR&date=                   the store's bills for a day, newest first
  GET  /holds?store=HSR                         frames reserved by bills being paid
  GET  /refunds?store=HSR&date=
  GET  /refunds/lookup?invoice_no=&store=       a bill to refund: lines, refunds so far (reads pre-cutover bills from Shopify)

Hosted demo only (python run.py serve without ELUNO_POS_BASE_URL; see api/sandbox.py):
  GET  /admin/sandbox                           mock failure mode, injected faults armed, recent fault log
  POST /admin/faults   {"fault": name|"clear"}  arm a failure the mock cannot produce (declined card, dead terminal, ...)
  POST /admin/reset    {"flaky"?: bool}         fresh mock + empty POS database

Every response carries X-Eluno-Network: the upstream calls the request made, with status and any retry wait.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import re
import threading
import time
import traceback
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse

from ..adapters.http import trace
from ..app import PosApp
from ..app.billing import BillRequest
from ..config import Settings
from ..domain.errors import DomainError, InvalidRequest, NotFound
from ..domain.parties import CHANNELS
from ..domain.refund import FAILED as REFUND_FAILED, refunded_line_refs
from ..invoice_render import render_credit_note_text, render_html, render_text
from ..ports import IntegrationError

log = logging.getLogger("eluno_pos.api")
WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")


class Api:
    def __init__(self, app: Optional[PosApp] = None, sandbox: Any = None):
        self._app = app
        self.sandbox = sandbox  # api.sandbox.Sandbox in the hosted demo; it owns (and can replace) the app
        self._tls = threading.local()
        # Requests run in parallel (ThreadingHTTPServer): a customer approving UPI at one store must not make
        # another store wait. The database serialises its own transactions; BillingService locks per bill.
        self.routes: List[Tuple[str, "re.Pattern[str]", Callable]] = []
        r = self.routes
        for method, pattern, fn in [
            ("GET", r"/health", self.health),
            ("GET", r"/meta", self.meta),
            ("GET", r"/stores", self.stores),
            ("GET", r"/staff", self.staff),
            ("GET", r"/catalog", self.catalog),
            ("GET", r"/bills", self.list_bills),
            ("GET", r"/holds", self.holds),
            ("GET", r"/refunds", self.list_refunds),
            ("GET", r"/refunds/lookup", self.refund_lookup),
            ("GET", r"/admin/sandbox", self.sandbox_status),
            ("POST", r"/admin/faults", self.sandbox_fault),
            ("POST", r"/admin/reset", self.sandbox_reset),
            ("GET", r"/customers/(?P<phone>[^/]+)", self.customer),
            ("POST", r"/customers", self.create_customer),
            ("POST", r"/customers/(?P<phone>[^/]+)/prescriptions", self.add_prescription),
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
            ("GET", r"/reports/all-stores", self.all_stores),
            ("GET", r"/credit-notes/(?P<cn>[^/]+)\.txt", self.credit_note),
            ("GET", r"/audit", self.audit),
        ]:
            r.append((method, re.compile("^%s$" % pattern), fn))

    @property
    def app(self) -> PosApp:
        return getattr(self._tls, "app", None) or self._app  # type: ignore[return-value]

    def dispatch(self, method: str, raw_path: str, body: Optional[dict]) -> Tuple[int, Any, str]:
        u = urlparse(raw_path)
        query = {k: v[0] for k, v in parse_qs(u.query).items()}
        for m, rx, fn in self.routes:
            match = rx.match(u.path)
            if match and m == method:
                params = {k: unquote(v) for k, v in match.groupdict().items()}
                if self.sandbox is None or u.path.startswith("/admin/"):
                    return fn(body or {}, query, **params)
                with self.sandbox.using() as app:  # a reset waits for this request to finish
                    self._tls.app = app
                    try:
                        return fn(body or {}, query, **params)
                    finally:
                        self._tls.app = None
        raise NotFound("no route %s %s" % (method, u.path))

    # ------------------------------------------------------------ handlers
    def health(self, body, q):
        return 200, {"ok": True}, "json"

    def meta(self, body, q):
        a = self.app
        return 200, {"business_date": a.clock.today().isoformat(), "now": a.clock.iso(), "channels": list(CHANNELS),
                     "payment_methods": ["upi", "card", "cash"], "reason_codes": a.config.reason_codes,
                     "offer_policy": a.config.setting("offer_policy", "best_for_customer"), "sandbox": self.sandbox is not None}, "json"

    def stores(self, body, q):
        return 200, {"stores": [{"store_code": s.store_code, "name": s.name, "address": s.address, "invoice_prefix": s.invoice_prefix,
                                 "location_id": s.location_id} for s in self.app.directory.stores() if s.is_billing_store]}, "json"

    def staff(self, body, q):
        store = self.app.directory.store(_req(q, "store"))
        return 200, {"staff": [{"staff_id": s.staff_id, "name": s.name, "role": s.role, "badges": list(s.badges),
                                "can_override_discount": s.can_override_discount}
                               for s in self.app.store_tool.staff(store.store_code)]}, "json"

    def catalog(self, body, q):
        """What the till can sell at a store right now. Stock is read live; frames being paid for elsewhere are subtracted."""
        a = self.app
        store = a.directory.store(_req(q, "store"))
        cat = a.commerce.catalog()
        frames = [f for f in cat.frames.values() if f.sellable]
        items = [f.inventory_item_id for f in frames if f.inventory_item_id]
        levels = a.commerce.stock_levels(items)
        held = a.repo.holds([(i, store.location_id) for i in items])
        others = {s.location_id: s.store_code for s in a.directory.stores() if s.is_billing_store and s.store_code != store.store_code}
        out_frames = []
        for f in sorted(frames, key=lambda f: f.sku):
            key = (f.inventory_item_id, store.location_id)
            in_shopify, on_hold = levels.get(key, 0), held.get(key, 0)
            out_frames.append({"sku": f.sku, "inventory_item_id": f.inventory_item_id, "title": f.title, "variant": f.variant_title, "description": f.description, "mrp": f.mrp,
                               "category": f.category, "rx_able": f.rx_able, "in_shopify": in_shopify, "held": on_hold,
                               "available": max(0, in_shopify - on_hold),
                               "elsewhere": {code: levels[(f.inventory_item_id, loc)] for loc, code in others.items()
                                             if levels.get((f.inventory_item_id, loc), 0) > 0}})
        lenses = [{"code": "%s/%s/%s" % (l.lens_type, l.index, l.coating), "type": l.lens_type, "index": l.index, "coating": l.coating,
                   "label": l.label, "mrp": l.mrp, "lab_tat_days": l.lab_tat_days,
                   "limits": {"sph_min": l.limits.sph_min, "sph_max": l.limits.sph_max, "cyl_abs_max": l.limits.cyl_abs_max,
                              "add_required": l.limits.add_required}}
                  for l in sorted(cat.lenses.values(), key=lambda l: (l.lens_type, l.index, l.coating))]
        return 200, {"store_code": store.store_code, "frames": out_frames, "lenses": lenses}, "json"

    def list_bills(self, body, q):
        a = self.app
        store = a.directory.store(_req(q, "store"))
        day = q.get("date") or a.clock.today().isoformat()
        refunded: Dict[str, int] = {}
        for r in a.repo.refunds(store_code=store.store_code):
            if r.status != REFUND_FAILED:
                refunded[r.bill_id] = refunded.get(r.bill_id, 0) + r.amount
        bills = [b for b in a.repo.bills(store_code=store.store_code, business_date=day) if b.source == "eluno-pos"]
        return 200, {"store_code": store.store_code, "date": day,
                     "bills": [_bill_summary(b, refunded.get(b.bill_id, 0)) for b in reversed(bills)]}, "json"

    def holds(self, body, q):
        store = self.app.directory.store(_req(q, "store"))
        return 200, {"holds": self.app.repo.hold_list(store.location_id)}, "json"

    def list_refunds(self, body, q):
        a = self.app
        store = a.directory.store(_req(q, "store"))
        day = q.get("date") or a.clock.today().isoformat()
        return 200, {"refunds": [r.to_dict() for r in reversed(a.repo.refunds(store_code=store.store_code, day=day))]}, "json"

    def refund_lookup(self, body, q):
        a = self.app
        store = a.directory.store(_req(q, "store"))
        bill = a.refunds.find_bill(_req(q, "invoice_no").strip(), store)
        refunds = a.repo.refunds(bill_id=bill.bill_id)
        return 200, {"bill": bill.to_dict(), "refunds": [r.to_dict() for r in refunds],
                     "refunded_line_refs": sorted(refunded_line_refs(refunds))}, "json"

    # ------------------------------------------------------------ hosted demo
    def _sandbox(self):
        if self.sandbox is None:
            raise NotFound("Not running as the hosted demo: there is no mock to control.", code="no_sandbox")
        return self.sandbox

    def sandbox_status(self, body, q):
        return 200, self._sandbox().status(), "json"

    def sandbox_fault(self, body, q):
        sb = self._sandbox()
        name = _req(body, "fault")
        try:
            sb.arm(name)
        except KeyError:
            raise InvalidRequest("Unknown fault %r." % name, code="bad_fault")
        return 200, sb.status(), "json"

    def sandbox_reset(self, body, q):
        sb = self._sandbox()
        flaky = body.get("flaky")
        sb.reset(None if flaky is None else bool(flaky))
        return 200, dict(sb.status(), cutover=sb.cutover), "json"

    def customer(self, body, q, phone):
        return 200, self.app.billing.lookup(phone), "json"

    def create_customer(self, body, q):
        c = self.app.customers.register(_req(body, "store_code"), _req(body, "staff_id"), body.get("first_name", ""), body.get("last_name", ""),
                                        _req(body, "phone"), body.get("email"))
        return 201, self.app.billing.lookup(c.phone), "json"

    def add_prescription(self, body, q, phone):
        rx = self.app.customers.add_prescription(_req(body, "store_code"), _req(body, "staff_id"), phone, _req(body, "source"), body.get("issued_on"),
                                                 _req(body, "od"), _req(body, "os"), body.get("pd"), body.get("notes"), body.get("optometrist_id"))
        return 201, dict(self.app.billing.lookup(rx.phone), recorded_rx_id=rx.rx_id), "json"

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

    def all_stores(self, body, q):
        return 200, self.app.reports.all_stores(date.fromisoformat(q["date"]) if q.get("date") else self.app.clock.today()), "json"

    def credit_note(self, body, q, cn):
        r = self.app.repo.get_refund_by_credit_note(cn)
        if r is None:
            raise NotFound("No credit note %s." % cn)
        bill = self.app.repo.get_bill(r.bill_id)
        return 200, render_credit_note_text(r, bill, self.app.directory.store(bill.store_code)), "txt"

    def audit(self, body, q):
        return 200, {"events": self.app.repo.audit_log(ref=q.get("ref"), store_code=q.get("store"), limit=int(q.get("limit", 100)))}, "json"


def _bill_summary(b: Any, refunded: int) -> Dict[str, Any]:
    return {"bill_id": b.bill_id, "status": b.status, "kind": b.kind, "invoice_no": b.invoice_no, "customer_name": b.customer_name,
            "customer_phone": b.customer_phone, "staff_id": b.staff_id, "channel": b.channel, "total": b.total,
            "payment_method": b.payment_method, "charge_id": b.charge_id, "shopify_order_name": b.shopify_order_name,
            "created_at": b.created_at, "paid_at": b.paid_at, "last_error": b.last_error, "refunded": refunded,
            "items": [l.description for l in b.lines]}


def _req(d: Dict[str, Any], key: str) -> Any:
    if d.get(key) in (None, ""):
        raise InvalidRequest("%s is required" % key, code="missing_field")
    return d[key]


def _bill_request(body: dict) -> BillRequest:
    try:
        return BillRequest.from_dict(body)
    except (KeyError, TypeError) as e:
        raise InvalidRequest("bad bill request: %s" % e, code="bad_request")


def make_handler(api: Api, web_dir: Optional[str] = WEB):
    class Handler(BaseHTTPRequestHandler):
        server_version = "eluno-pos/1.0"
        network: List[Dict[str, Any]] = []
        started = 0.0

        def _respond(self, status: int, payload: Any, fmt: str = "json") -> None:
            if fmt == "json":
                data, ctype = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8"
            else:
                data, ctype = payload.encode("utf-8"), "text/html; charset=utf-8" if fmt == "html" else "text/plain; charset=utf-8"
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Eluno-Network", json.dumps(self.network[-40:]))
            self.send_header("X-Eluno-Ms", str(int((time.monotonic() - self.started) * 1000)))
            self.end_headers()
            self.wfile.write(data)

        def _handle(self, method: str) -> None:
            self.started, self.network = time.monotonic(), []
            body = None
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError:
                    return self._respond(400, {"error": {"code": "bad_json", "message": "body is not valid JSON"}})
            with trace() as self.network:
                try:
                    status, payload, fmt = api.dispatch(method, self.path, body)
                except DomainError as e:
                    status, payload, fmt = e.http_status, {"error": e.to_dict()}, "json"
                except IntegrationError as e:
                    status, payload, fmt = 502, {"error": {"code": "upstream_error", "message": str(e), "retryable": e.retryable}}, "json"
                except Exception as e:  # pragma: no cover - last resort, keep the till informed
                    log.error("unhandled error: %s\n%s", e, traceback.format_exc())
                    status, payload, fmt = 500, {"error": {"code": "internal_error", "message": str(e)}}, "json"
            self._respond(status, payload, fmt)

        def _static(self) -> bool:
            """The till screen: / is index.html, /web/<file> its assets. Nothing outside web/ is served."""
            if not web_dir or not os.path.isdir(web_dir):
                return False
            path = urlparse(self.path).path
            if path == "/":
                path = "/web/index.html"
            if not path.startswith("/web/"):
                return False
            root = os.path.realpath(web_dir)
            full = os.path.realpath(os.path.join(root, unquote(path[len("/web/"):])))
            if not full.startswith(root + os.sep) or not os.path.isfile(full):
                return False
            with open(full, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
            return True

        def do_GET(self) -> None:
            if self._static():
                return
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def log_message(self, fmt: str, *args: Any) -> None:
            log.info("%s %s", self.command, self.path)

    return Handler


def serve(settings: Settings, port: int = 8000, host: str = "127.0.0.1", sandbox: bool = False, flaky: bool = True,
          fail_rate: float = 0.5) -> None:
    """sandbox=True: the hosted demo. This process starts its own mock (429/503 on unless flaky=False) and can reset it.
    sandbox=False: a till against an existing backend at settings.base_url."""
    shown = "127.0.0.1" if host in ("0.0.0.0", "") else host
    if sandbox:
        from .sandbox import Sandbox
        sb = Sandbox(settings, flaky=flaky, fail_rate=fail_rate)
        api, closer = Api(sandbox=sb), sb.close
        print("Eluno POS on http://%s:%d  (hosted-demo mode: own mock at %s, 429/503 %s, business date %s)" % (
            shown, port, sb.mock.url, "on, %d%% each" % (fail_rate * 100) if flaky else "off", sb.app.clock.today()))
        for series, info in sb.cutover.items():
            print("  series %-22s last issued %05d" % (series, info["last"]))
    else:
        app = PosApp(settings)
        api, closer = Api(app), app.close
        print("Eluno POS on http://%s:%d  (backend %s, db %s)" % (shown, port, settings.base_url, settings.db_path))
        try:
            for series, info in app.billing.cutover().items():
                print("  series %-22s last issued %05d" % (series, info["last"]))
        except Exception as e:  # Shopify down at start-up: billing still refuses safely per store until it is back
            print("  cutover check failed (%s); stores without an initialised series will refuse payment until it succeeds" % e)
        for line in app.sync_pending():
            print("  startup sync: " + line)
    print("  till screen: http://%s:%d/" % (shown, port), flush=True)
    server = ThreadingHTTPServer((host, port), make_handler(api))
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        closer()
