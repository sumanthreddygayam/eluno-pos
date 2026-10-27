"""The hosted demo: one process runs the starter-kit mock and the till against it.

Not a production component. In production the till talks to the real Shopify, store tool and terminal, and
none of this exists. Here it lets a reviewer:
  * run with the mock's own 429/503 failures on (the default) or off,
  * switch on the failures the mock cannot produce (a declined card, a dead terminal, Shopify refusing
    orders, a Wi-Fi drop that loses Shopify's answer, a customer slow to approve UPI),
  * reset everything to the seed data between sessions.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Dict, Iterator, Optional

from ..app import PosApp
from ..config import ROOT, Settings
from ..devtools import FaultInjector, MockServer, free_port

# name -> (what the reviewer sees, how to arm it on the injector)
FAULTS: Dict[str, Dict[str, Any]] = {
    "decline_next_payment": {
        "label": "Next card/UPI payment is declined by the bank",
        "arm": lambda f: f.reject("POST", "/terminal/charges", 402, {"error": "declined by the customer's bank"}, times=1),
    },
    "terminal_unreachable": {
        "label": "Card machine stops answering (one payment attempt)",
        "arm": lambda f: f.outage("POST", "/terminal/charges", times=5),  # every retry of one Pay; the next Pay goes through
    },
    "slow_upi": {
        "label": "Customer takes 6 s to approve UPI on their phone",
        "arm": lambda f: f.delay("POST", "/terminal/charges", 6.0, times=1),
    },
    "wifi_drop_after_order": {
        "label": "Wi-Fi drops: Shopify creates the next order but its answer is lost",
        "arm": lambda f: f.drop_response("POST", "orders.json", times=1),
    },
    "shopify_refuses_orders": {
        "label": "Shopify unreachable for new orders (until cleared)",
        "arm": lambda f: f.outage("POST", "orders.json"),
    },
}


def _sample_customers() -> list:
    """Seed customers, so a reviewer can pick someone without knowing the synthetic phone numbers."""
    try:
        with open(os.path.join(ROOT, "starter-kit", "data", "customers.json"), encoding="utf-8") as f:
            return [{"phone": c["phone"], "name": ("%s %s" % (c.get("first_name", ""), c.get("last_name", ""))).strip(),
                     "tags": [t.strip() for t in (c.get("tags") or "").split(",") if t.strip()]} for c in json.load(f)]
    except (OSError, ValueError, KeyError):
        return []


class Sandbox:
    def __init__(self, settings: Settings, flaky: bool = True):
        self.base_settings = settings
        self.flaky = flaky
        self.faults = FaultInjector()
        self.mock: Optional[MockServer] = None
        self.app: Optional[PosApp] = None
        self._dir: Optional[str] = None
        self._cond = threading.Condition()
        self._active = 0
        self._resetting = False
        self.cutover: Dict[str, Dict[str, int]] = {}
        self._start()

    # ------------------------------------------------------------ lifecycle
    def _start(self) -> None:
        self.mock = MockServer(flaky=self.flaky, port=free_port(), today=self.base_settings.business_date).__enter__()
        self._dir = tempfile.mkdtemp(prefix="eluno-pos-")
        settings = replace(self.base_settings, base_url=self.mock.url, db_path=os.path.join(self._dir, "pos.db"))
        self.faults.clear()
        self.app = PosApp(settings, transport=self.faults)
        self.cutover = self.app.billing.cutover()

    def _stop(self) -> None:
        if self.app:
            self.app.close()
        if self.mock:
            self.mock.__exit__()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self.app = self.mock = self._dir = None

    def reset(self, flaky: Optional[bool] = None) -> None:
        """Back to the seed: a fresh mock (Shopify, store tool, terminal) and an empty POS database."""
        with self._cond:
            self._resetting = True
            while self._active:  # let requests already running finish on the old app
                self._cond.wait()
            try:
                self._stop()
                if flaky is not None:
                    self.flaky = flaky
                self._start()
            finally:
                self._resetting = False
                self._cond.notify_all()

    def close(self) -> None:
        with self._cond:
            self._stop()

    @contextmanager
    def using(self) -> Iterator[PosApp]:
        with self._cond:
            while self._resetting:
                self._cond.wait()
            self._active += 1
            app = self.app
        try:
            yield app  # type: ignore[misc]
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()

    # ------------------------------------------------------------ faults
    def arm(self, name: str) -> None:
        if name == "clear":
            self.faults.clear()
            return
        if name not in FAULTS:
            raise KeyError(name)
        FAULTS[name]["arm"](self.faults)

    def status(self) -> Dict[str, Any]:
        live = [r for r in self.faults.rules if r["left"] > 0]
        return {"mock_flaky": self.flaky, "mock_url": self.mock.url if self.mock else None,
                "available": [{"name": k, "label": v["label"]} for k, v in FAULTS.items()],
                "armed": [{"kind": r["kind"], "call": "%s %s" % (r["method"], r["part"]), "left": r["left"]} for r in live],
                "log": self.faults.log[-20:], "sample_customers": _sample_customers()}
