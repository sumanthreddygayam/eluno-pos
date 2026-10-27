"""A small JSON-over-HTTP client with an explicit retry policy.

Which failures are safe to retry depends on the call, so the caller says so:
  * 429 Too Many Requests: the server rejected the request before doing anything; always retried,
    after Retry-After.
  * GET, and POSTs the remote system documents as idempotent (terminal charge by order_ref):
    retried on 5xx and on network errors.
  * Other POSTs (Shopify order/refund create): a 5xx or a lost response means "maybe applied",
    so the client raises AmbiguousWrite and the adapter checks before trying again.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Tuple

from ..ports import AmbiguousWrite, IntegrationError, UpstreamRejected

log = logging.getLogger("eluno_pos.http")

Response = Tuple[int, Dict[str, str], bytes]
Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Response]


class TransportError(Exception):
    """The request may or may not have reached the server (timeout, reset, refused)."""

    def __init__(self, message: str, sent: bool = True):
        super().__init__(message)
        self.sent = sent  # False when we know nothing left this machine (connection refused / DNS)


def urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float) -> Response:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read() or b""
    except urllib.error.URLError as e:
        refused = isinstance(e.reason, ConnectionRefusedError) or isinstance(e.reason, socket.gaierror)
        raise TransportError(str(e.reason), sent=not refused)
    except (socket.timeout, TimeoutError, ConnectionError) as e:
        raise TransportError("%s: %s" % (type(e).__name__, e))


_trace = threading.local()


@contextmanager
def trace() -> Iterator[List[Dict[str, Any]]]:
    """Collect every upstream attempt made on this thread (service, call, status, wait before retry).

    The till API returns it with each response, so the screen can show a 429 or 503 being absorbed."""
    events: List[Dict[str, Any]] = []
    outer = getattr(_trace, "events", None)
    _trace.events = events
    try:
        yield events
    finally:
        _trace.events = outer


def _record(service: str, method: str, url: str, status: Any, started: float) -> Optional[Dict[str, Any]]:
    events = getattr(_trace, "events", None)
    if events is None:
        return None
    path = urllib.parse.urlparse(url).path
    e = {"service": service, "call": "%s %s" % (method, path.replace("/admin/api/2026-10/", "/")), "status": status,
         "ms": int((time.monotonic() - started) * 1000)}
    events.append(e)
    return e


@dataclass(frozen=True)
class RetryPolicy:
    # Sized for the 10-second checkout budget: a dead upstream fails over to the outbox in ~2 s.
    max_attempts: int = 5
    backoff_base: float = 0.15
    backoff_max: float = 2.0
    retry_after_cap: float = 10.0

    def backoff(self, attempt: int) -> float:
        return min(self.backoff_max, self.backoff_base * (2 ** (attempt - 1))) * (0.5 + random.random() / 2)


class HttpClient:
    def __init__(self, base_url: str, service: str, transport: Optional[Transport] = None,
                 policy: RetryPolicy = RetryPolicy(), timeout: float = 8.0, sleep: Callable[[float], None] = time.sleep):
        self.base_url = base_url.rstrip("/")
        self.service = service
        self.transport = transport or urllib_transport
        self.policy = policy
        self.timeout = timeout
        self.sleep = sleep
        self.calls = 0
        self.retries = 0

    def url(self, path: str, params: Optional[Mapping[str, Any]] = None) -> str:
        u = "%s/%s" % (self.base_url, path.lstrip("/"))
        if params:
            u += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        return u

    def get(self, path: str, params: Optional[Mapping[str, Any]] = None) -> Any:
        return self._send("GET", self.url(path, params), None, idempotent=True)[0]

    def get_all(self, path: str, params: Mapping[str, Any], key: str, max_pages: int = 50) -> list:
        """Follows Shopify-style cursor pagination (Link: <...>; rel="next") and concatenates `key`."""
        out: list = []
        url: Optional[str] = self.url(path, params)
        pages = 0
        while url and pages < max_pages:
            payload, headers = self._send("GET", url, None, idempotent=True)
            out.extend((payload or {}).get(key, []))
            url = _next_link(headers.get("link", ""))
            pages += 1
        return out

    def post(self, path: str, body: Any, idempotent: bool) -> Any:
        return self._send("POST", self.url(path), body, idempotent=idempotent)[0]

    def _send(self, method: str, url: str, body: Any, idempotent: bool) -> Tuple[Any, Dict[str, str]]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        attempt = 0
        while True:
            attempt += 1
            self.calls += 1
            last = attempt >= self.policy.max_attempts
            started = time.monotonic()
            try:
                status, rheaders, raw = self.transport(method, url, headers, data, self.timeout)
            except TransportError as e:
                ev = _record(self.service, method, url, "no response", started)
                if not e.sent or idempotent:
                    if last:
                        raise IntegrationError(self.service, "%s %s failed after %d attempts: %s" % (method, url, attempt, e))
                    self._pause(self.policy.backoff(attempt), "%s %s: %s" % (method, url, e), ev)
                    continue
                raise AmbiguousWrite(self.service, "%s %s: no response (%s); the write may have happened" % (method, url, e))
            ev = _record(self.service, method, url, status, started)
            payload = _decode(raw)
            if 200 <= status < 300:
                if self.service == "shopify":  # only Shopify's own bucket counts (the mock stamps the header on every answer)
                    self._watch_call_limit(rheaders)
                return payload, rheaders
            if status == 429:
                if last:
                    raise IntegrationError(self.service, "rate limited on %s %s after %d attempts" % (method, url, attempt), status, payload)
                self._pause(self._retry_after(rheaders, attempt), "429 on %s %s" % (method, url), ev)
                continue
            if status >= 500:
                if idempotent and not last:
                    self._pause(self.policy.backoff(attempt), "%d on %s %s" % (status, method, url), ev)
                    continue
                if not idempotent and method != "GET":
                    raise AmbiguousWrite(self.service, "%d on %s %s; the write may have happened" % (status, method, url), status, payload)
                raise IntegrationError(self.service, "%d on %s %s after %d attempts" % (status, method, url, attempt), status, payload)
            raise UpstreamRejected(self.service, "%d on %s %s: %s" % (status, method, url, _errors(payload)), status, payload)

    def _retry_after(self, headers: Dict[str, str], attempt: int) -> float:
        try:
            return min(float(headers.get("retry-after", "")), self.policy.retry_after_cap)
        except ValueError:
            return self.policy.backoff(attempt)

    def _pause(self, seconds: float, why: str, event: Optional[Dict[str, Any]] = None) -> None:
        self.retries += 1
        if event is not None:
            event["retry_in"] = round(seconds, 2)
        log.info("%s: retrying in %.2fs (%s)", self.service, seconds, why)
        self.sleep(seconds)

    def _watch_call_limit(self, headers: Dict[str, str]) -> None:
        """Shopify's leaky bucket: slow down a little before the bucket is full instead of eating 429s."""
        limit = headers.get("x-shopify-shop-api-call-limit")
        if not limit:
            return
        try:
            used, size = (int(x) for x in limit.split("/"))
        except ValueError:
            return
        if size and used >= size - 2:
            self.sleep(0.5)


def _next_link(link: str) -> Optional[str]:
    for part in link.split(","):
        if 'rel="next"' in part and "<" in part and ">" in part:
            return part[part.index("<") + 1:part.index(">")]
    return None


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {"raw": raw[:500].decode("utf-8", "replace")}


def _errors(payload: Any) -> str:
    if isinstance(payload, dict):
        for k in ("errors", "error"):
            if k in payload:
                return json.dumps(payload[k]) if not isinstance(payload[k], str) else payload[k]
    return json.dumps(payload)[:300]
