"""Development helpers: run the starter-kit mock as a child process, and inject network faults.

Not used in production code paths; the demo and the integration tests use them.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Dict, List, Optional

from .adapters.http import Response, TransportError, urllib_transport
from .config import ROOT

MOCK = os.path.join(ROOT, "starter-kit", "mock-apis", "mock_server.py")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class MockServer:
    """`with MockServer(flaky=True) as m: ... m.url ...` - a fresh in-memory Shopify/SMT/terminal."""

    def __init__(self, flaky: bool = True, port: Optional[int] = None, today: Optional[str] = None):
        self.flaky = flaky
        self.today = today
        self.port = port or free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.proc: Optional[subprocess.Popen] = None

    def __enter__(self) -> "MockServer":
        args = [sys.executable, MOCK, "--port", str(self.port)] + ([] if self.flaky else ["--no-flake"]) + (["--today", self.today] if self.today else [])
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=os.path.dirname(MOCK))
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.url + "/health", timeout=1).read()
                return self
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError("mock server exited with %s" % self.proc.returncode)
                time.sleep(0.1)
        self.__exit__()
        raise RuntimeError("mock server did not start on %s" % self.url)

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


class FaultInjector:
    """A transport wrapper. Rules match on method + URL substring.

    drop_response(n): the request reaches the server and is processed, then the response is lost
                      (what a Wi-Fi drop mid-request looks like).
    outage(n):        the server answers 503 without processing.
    """

    def __init__(self, inner=urllib_transport):
        self.inner = inner
        self.rules: List[Dict] = []
        self.log: List[str] = []

    def drop_response(self, method: str, url_part: str, times: int = 1) -> None:
        self.rules.append({"method": method, "part": url_part, "kind": "drop", "left": times})

    def outage(self, method: str, url_part: str, times: int = 1_000_000) -> None:
        self.rules.append({"method": method, "part": url_part, "kind": "outage", "left": times})

    def delay(self, method: str, url_part: str, seconds: float, times: int = 1) -> None:
        """The call works, but slowly (a terminal waiting for the customer to approve on their phone)."""
        self.rules.append({"method": method, "part": url_part, "kind": "delay", "left": times, "seconds": seconds})

    def reject(self, method: str, url_part: str, status: int = 402, body: Optional[dict] = None, times: int = 1, after: float = 0.0) -> None:
        """The server answers with a definite refusal (e.g. card declined) without doing anything, optionally after `after` seconds."""
        self.rules.append({"method": method, "part": url_part, "kind": "reject", "left": times, "status": status,
                           "body": body or {"error": "rejected"}, "after": after})

    def clear(self) -> None:
        self.rules.clear()

    def __call__(self, method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Response:
        for r in self.rules:
            if r["left"] > 0 and r["method"] == method and r["part"] in url:
                r["left"] -= 1
                if r["kind"] == "outage":
                    self.log.append("503 (injected outage) %s %s" % (method, url))
                    return 503, {}, b'{"errors":"injected outage"}'
                if r["kind"] == "delay":
                    self.log.append("slow (%.1fs) %s %s" % (r["seconds"], method, url))
                    time.sleep(r["seconds"])
                    return self.inner(method, url, headers, body, timeout)
                if r["kind"] == "reject":
                    if r.get("after"):
                        time.sleep(r["after"])
                    self.log.append("%d (injected refusal) %s %s" % (r["status"], method, url))
                    return r["status"], {}, json.dumps(r["body"]).encode()
                status, h, raw = self.inner(method, url, headers, body, timeout)
                if not 200 <= status < 300:  # only lose responses of writes the server actually applied
                    r["left"] += 1
                    return status, h, raw
                self.log.append("server applied the write (%d) but the response was lost: %s %s" % (status, method, url))
                raise TransportError("injected: connection dropped before the response arrived")
        return self.inner(method, url, headers, body, timeout)
