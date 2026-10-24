"""Checkout timing and load: where the 10 seconds go, and what happens with more tills.

    python run.py bench

The local mock answers in ~1 ms, which says nothing about a store in Bengaluru. This bench wraps the
real HTTP transport in `SimNet`, which adds a network delay per call and enforces Shopify's REST rate limit
(a leaky bucket shared by every till of the shop: 40 requests, draining at 2 per second on standard plans;
Shopify Plus is 400 / 20 per second). The delays are ASSUMPTIONS, printed with every result; change them
with the flags below. Everything else (offers, tax, holds, invoice numbers, retries) is the real code.

    --shopify-ms 400   --smt-ms 150   --terminal-ms 800   (round trip per call, from the store)
"""
from __future__ import annotations

import math
import os
import statistics
import sys
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

from .adapters.http import urllib_transport
from .app import PosApp
from .app.billing import BillRequest, PairRequest
from .config import Settings
from .day import shopify_set_stock
from .devtools import FaultInjector, MockServer

DAY = "2026-10-22"
STORES = [("MM", 70000001, "S101"), ("HSR", 70000002, "S105"), ("BR", 70000003, "S109"), ("KMG", 70000004, "S113"),
          ("JN", 70000005, "S117"), ("IN", 70000006, "S121"), ("VC", 70000007, "S125")]
# customers with a valid prescription that a single vision 1.67 lens can be made for (Suresh's axis needs a CONFIRM; left out)
PHONES = ["+919000000001", "+919000000002", "+919000000004", "+919000000005", "+919000000007", "+919000000009", "+919000000010",
          "+919000000011", "+919000000013", "+919000000015", "+919000000016", "+919000000017", "+919000000018", "+919000000020",
          "+919000000021", "+919000000022", "+919000000023", "+919000000025", "+919000000027", "+919000000029"]
FRAMES = ["FR-%04d" % i for i in range(1, 40) if i not in (4,)]


class SimNet:
    """Transport wrapper: per-call network delay + Shopify's leaky-bucket rate limit (shared by all tills)."""

    def __init__(self, inner, shopify_ms: float, smt_ms: float, terminal_ms: float, bucket: int = 40, leak_per_s: float = 2.0):
        self.inner, self.bucket, self.leak = inner, bucket, leak_per_s
        self.delay = {"shopify": shopify_ms / 1000, "smt": smt_ms / 1000, "terminal": terminal_ms / 1000}
        self.level, self.last = 0.0, time.monotonic()
        self.lock = threading.Lock()
        self.calls: List[Tuple[float, float, str, str, str, int]] = []  # (start, end, service, method, path, status)
        self.throttled = 0

    @staticmethod
    def service(url: str) -> str:
        return "shopify" if "/admin/api/" in url else "smt" if "/smt/" in url else "terminal" if "/terminal/" in url else "other"

    def __call__(self, method, url, headers, body, timeout):
        svc, start = self.service(url), time.monotonic()
        used = None
        if svc == "shopify":
            with self.lock:
                now = time.monotonic()
                self.level = max(0.0, self.level - (now - self.last) * self.leak)
                self.last = now
                if self.level + 1 > self.bucket:
                    self.throttled += 1
                    wait = (self.level + 1 - self.bucket) / self.leak
                    time.sleep(self.delay[svc])  # the 429 still costs a round trip
                    self.calls.append((start, time.monotonic(), svc, method, _path(url), 429))
                    return 429, {"retry-after": "%.2f" % wait}, b'{"errors":"Exceeded 2 calls per second for api client."}'
                self.level += 1
                used = self.level
        time.sleep(self.delay.get(svc, 0))
        status, h, raw = self.inner(method, url, headers, body, timeout)
        if used is not None:
            h = dict(h)
            h["x-shopify-shop-api-call-limit"] = "%d/%d" % (math.ceil(used), self.bucket)
        self.calls.append((start, time.monotonic(), svc, method, _path(url), status))
        return status, h, raw


def _path(url: str) -> str:
    p = url.split("?")[0].split("/", 3)[3]
    return p.replace("admin/api/2026-10/", "")


def pct(xs: List[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def flag(argv, name: str, default: float) -> float:
    if name in argv:
        return float(argv[argv.index(name) + 1])
    return default


# ---------------------------------------------------------------------- scenarios
def one_bill_steps(url: str, folder: str, lat: Dict[str, float]) -> None:
    print("\n1. ONE BILL, STEP BY STEP (after the customer says yes; no failures)")
    net = SimNet(urllib_transport, lat["shopify"], lat["smt"], lat["terminal"])
    app = PosApp(Settings(base_url=url, db_path=os.path.join(folder, "steps.db"), business_date=DAY), transport=net)
    try:
        app.billing.cutover(["HSR"])
        req = BillRequest("HSR", "S105", "+919000000005", "walk_in", (PairRequest("FR-0003", "progressive/1.60/blue_cut"),))
        app.billing.quote(req)  # what the customer was shown before saying yes (warms the catalog cache)
        net.calls.clear()
        t0 = time.monotonic()
        bill, _ = app.billing.create(req)
        t1 = time.monotonic()
        bill = app.billing.pay(bill.bill_id, "upi", "S105")
        t2 = time.monotonic()
        calls = list(net.calls)
    finally:
        app.close()
    rows = []

    def window(lo: float, hi: float) -> List[tuple]:
        return [c for c in calls if lo <= c[0] < hi]

    create_calls = window(t0, t1)
    pay_calls = window(t1, t2)
    term = [c for c in pay_calls if c[2] == "terminal"]
    order = [c for c in pay_calls if c[4] == "orders.json" and c[3] == "POST"]
    recheck = [c for c in pay_calls if c[0] < term[0][0]]
    rows.append(("A  Save the bill (re-price + save draft)", create_calls, t1 - t0))
    rows.append(("B  Re-check price/stock + reserve frame", recheck, (term[0][0] - t1)))
    rows.append(("C  Terminal: charge request/response", term, term[-1][1] - term[0][0]))
    rows.append(("D  Invoice number + PAID (one DB transaction)", [], order[0][0] - term[-1][1]))
    rows.append(("E  Shopify order create", order, order[-1][1] - order[0][0]))
    rows.append(("F  Release hold, audit, return", [], t2 - order[-1][1]))
    print("   %-48s %6s %9s   %s" % ("step", "calls", "time", "calls made"))
    for name, cs, dt in rows:
        print("   %-48s %6d %8.2fs   %s" % (name, len(cs), dt, ", ".join("%s %s" % (c[3], c[4]) for c in cs) or "(local only)"))
    shop = sum(1 for c in create_calls + pay_calls if c[2] == "shopify")
    print("   %-48s %6d %8.2fs   (%d Shopify, %d store tool, %d terminal)" % (
        "TOTAL system time, yes -> invoice", len(create_calls + pay_calls), t2 - t0, shop,
        sum(1 for c in create_calls + pay_calls if c[2] == "smt"), len(term)))
    print("   + the customer paying, NOT system time (estimates): UPI 15-30 s (sometimes 'pending' 1-2 min), card tap <= Rs 5,000 3-5 s,")
    print("     card + PIN > Rs 5,000 8-15 s (most bills; average Rs 5,400), cash 30-60 s (no terminal call)")
    print("   + printing the invoice: local, < 0.1 s to render, then the printer")


def flaky_bills(url: str, folder: str, lat: Dict[str, float], n: int = 20) -> None:
    print("\n2. %d BILLS IN A ROW WITH THE MOCK'S OWN FAILURES ON (10%% of Shopify writes 429, 30%% of new charges 503 once)" % n)
    net = SimNet(urllib_transport, lat["shopify"], lat["smt"], lat["terminal"])
    app = PosApp(Settings(base_url=url, db_path=os.path.join(folder, "flaky.db"), business_date=DAY), transport=net)
    times, retried = [], 0
    try:
        app.billing.cutover()
        for i in range(n):
            store, _, staff = STORES[i % len(STORES)]
            req = BillRequest(store, staff, PHONES[i % len(PHONES)], "walk_in", (PairRequest(FRAMES[i % len(FRAMES)], "single_vision/1.67/standard"),))
            before = sum(c.retries for c in app.http.values())
            t0 = time.monotonic()
            bill, _ = app.billing.create(req)
            app.billing.pay(bill.bill_id, "upi", staff)
            times.append(time.monotonic() - t0)
            retried += 1 if sum(c.retries for c in app.http.values()) > before else 0
    finally:
        app.close()
    print("   system time per bill: median %.2fs · p95 %.2fs · worst %.2fs · %d of %d bills needed a retry (each 429 waits ~1 s)" % (
        statistics.median(times), pct(times, 95), max(times), retried, n))


def burst(url: str, folder: str, lat: Dict[str, float], tills: int, bills_each: int, bucket: int, leak: float, label: str) -> None:
    net = SimNet(urllib_transport, lat["shopify"], lat["smt"], lat["terminal"], bucket=bucket, leak_per_s=leak)
    db = os.path.join(folder, "burst_%d_%d.db" % (tills, bucket))
    apps = [PosApp(Settings(base_url=url, db_path=db, business_date=DAY), transport=net) for _ in range(tills)]
    apps[0].billing.cutover()
    for a in apps:  # warm caches (catalog, stores, staff) as a till does at start of day
        a.commerce.catalog(), a.commerce.stores()
        for code, _, _ in STORES:
            a.store_tool.staff(code)
    net.level, net.throttled = 0.0, 0
    net.calls.clear()
    results: List[Optional[float]] = []
    errors: List[str] = []
    lock = threading.Lock()
    start_gate = threading.Event()

    def till(k: int) -> None:
        app = apps[k]
        store, _, staff = STORES[k % len(STORES)]
        start_gate.wait()
        for j in range(bills_each):
            i = k * bills_each + j
            req = BillRequest(store, staff, PHONES[i % len(PHONES)], "walk_in", (PairRequest(FRAMES[i % len(FRAMES)], "single_vision/1.67/standard"),))
            t0 = time.monotonic()
            try:
                bill, _ = app.billing.create(req)
                bill = app.billing.pay(bill.bill_id, "upi", staff)
                with lock:
                    results.append(time.monotonic() - t0)
                    if bill.status != "synced":
                        errors.append("%s left %s (Shopify sync pending)" % (bill.invoice_no, bill.status))
            except Exception as e:  # what the till would show
                with lock:
                    results.append(None)
                    errors.append("%s: %s" % (type(e).__name__, str(e)[:90]))

    threads = [threading.Thread(target=till, args=(k,)) for k in range(tills)]
    for t in threads:
        t.start()
    t0 = time.monotonic()
    start_gate.set()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0
    for a in apps:
        a.close()
    ok = [r for r in results if r is not None]
    shop_calls = sum(1 for c in net.calls if c[2] == "shopify" and c[5] != 429)
    print("   %-34s %3d tills x %d = %3d bills | median %5.2fs  p95 %5.2fs  worst %5.2fs | Shopify calls %4d, 429s %3d | %s" % (
        label, tills, bills_each, tills * bills_each, statistics.median(ok) if ok else 0, pct(ok, 95), max(ok) if ok else 0,
        shop_calls, net.throttled, ("%d problem(s): %s" % (len(errors), errors[0])) if errors else "all synced, wall %.0fs" % wall))


def wifi(url: str, folder: str, lat: Dict[str, float]) -> None:
    print("\n4. WI-FI / SHOPIFY TROUBLE DURING A BILL (same assumed latencies)")
    cases = [
        ("lost response on the order create (Wi-Fi blip)", lambda f: f.drop_response("POST", "/orders.json")),
        ("Shopify unreachable after payment (outage)", lambda f: (f.outage("POST", "/orders.json"), f.outage("GET", "/orders.json"))),
        ("terminal slow to answer (3 s)", lambda f: f.delay("POST", "/terminal/charges", 3.0)),
        ("terminal unreachable", lambda f: f.outage("POST", "/terminal/charges")),
    ]
    for i, (name, arm) in enumerate(cases):
        faults = FaultInjector(SimNet(urllib_transport, lat["shopify"], lat["smt"], lat["terminal"]))
        app = PosApp(Settings(base_url=url, db_path=os.path.join(folder, "wifi%d.db" % i), business_date=DAY), transport=faults)
        try:
            app.billing.cutover(["KMG"])
            req = BillRequest("KMG", "S113", PHONES[i], "walk_in", (PairRequest(FRAMES[10 + i], "single_vision/1.67/standard"),))
            bill, _ = app.billing.create(req)
            arm(faults)
            t0 = time.monotonic()
            try:
                bill = app.billing.pay(bill.bill_id, "upi", "S113")
                outcome = "%s %s" % (bill.status.upper(), bill.invoice_no)
            except Exception as e:
                outcome = "%s (bill %s)" % (getattr(e, "code", type(e).__name__), app.billing.get(bill.bill_id).status.upper())
            dt = time.monotonic() - t0
            faults.clear()
            print("   %-48s pay took %5.2fs -> %s" % (name, dt, outcome))
        finally:
            app.close()


def run(argv=()) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    import logging
    logging.basicConfig(level=logging.ERROR)
    lat = {"shopify": flag(argv, "--shopify-ms", 400), "smt": flag(argv, "--smt-ms", 150), "terminal": flag(argv, "--terminal-ms", 800)}
    folder = tempfile.mkdtemp(prefix="eluno_pos_bench_")
    print("#" * 100)
    print("#  CHECKOUT TIMING AND LOAD")
    print("#  Assumed round trip per call from a store: Shopify %(shopify).0f ms, store tool %(smt).0f ms, terminal %(terminal).0f ms" % lat)
    print("#  Shopify REST limit modelled as a leaky bucket per shop: 40 requests, draining 2/s (standard); Plus 400, 20/s")
    print("#" * 100)
    with MockServer(flaky=False, today=DAY) as clean, MockServer(flaky=True, today=DAY) as flaky:
        for m in (clean, flaky):
            for code, loc, _ in STORES:  # plenty of stock everywhere so the bench measures speed, not stock-outs
                for sku in FRAMES:
                    shopify_set_stock(m.url, 92000000 + int(sku[3:]), loc, 200)
        one_bill_steps(clean.url, folder, lat)
        flaky_bills(flaky.url, folder, lat)
        print("\n3. MANY TILLS PRESSING PAY AT THE SAME MOMENT (every till starts together; Shopify limit enforced)")
        burst(clean.url, folder, lat, 1, 3, 40, 2.0, "one till (baseline)")
        burst(clean.url, folder, lat, 7, 1, 40, 2.0, "one per store, same second")
        burst(clean.url, folder, lat, 14, 1, 40, 2.0, "two per store, same second")
        burst(clean.url, folder, lat, 28, 1, 40, 2.0, "four per store, same second")
        burst(clean.url, folder, lat, 28, 1, 400, 20.0, "four per store, Shopify Plus limit")
        burst(clean.url, folder, lat, 7, 4, 40, 2.0, "7 tills back to back (4 bills each)")
        wifi(clean.url, folder, lat)
    print("\nBench complete.")
    return 0
