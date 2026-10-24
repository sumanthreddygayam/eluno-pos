"""POS-owned state in SQLite: bills (the outbox to Shopify), refunds, the document register
(gapless invoice / credit-note series), till sessions and the audit trail.

Aggregates are stored as JSON next to the columns we query on. Number allocation runs inside
BEGIN IMMEDIATE, so two tills allocating at once serialize and never share or skip a number.
In production this is one central database (Postgres) shared by all store tills; the interface
stays the same.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from ..domain.bill import Bill
from ..domain.documents import format_number
from ..domain.errors import StateConflict
from ..domain.refund import Refund
from ..domain.till import OPEN, TillSession

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS bills (
    bill_id TEXT PRIMARY KEY, store_code TEXT NOT NULL, business_date TEXT NOT NULL, status TEXT NOT NULL,
    invoice_no TEXT UNIQUE, payload TEXT NOT NULL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS bills_store_day ON bills (store_code, business_date);
CREATE INDEX IF NOT EXISTS bills_status ON bills (status);
CREATE TABLE IF NOT EXISTS refunds (
    refund_id TEXT PRIMARY KEY, bill_id TEXT NOT NULL, store_code TEXT NOT NULL, status TEXT NOT NULL,
    credit_note_no TEXT UNIQUE, created_day TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT);
CREATE INDEX IF NOT EXISTS refunds_bill ON refunds (bill_id);
CREATE TABLE IF NOT EXISTS series (series TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, floor INTEGER NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
    number TEXT PRIMARY KEY, series TEXT NOT NULL, seq INTEGER NOT NULL, kind TEXT NOT NULL, store_code TEXT NOT NULL,
    ref TEXT, status TEXT NOT NULL, issued_at TEXT NOT NULL, note TEXT, UNIQUE (series, seq));
CREATE TABLE IF NOT EXISTS till_sessions (
    session_id TEXT PRIMARY KEY, store_code TEXT NOT NULL, status TEXT NOT NULL, opened_day TEXT NOT NULL, payload TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_open_till_per_store ON till_sessions (store_code) WHERE status = 'open';
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, ref TEXT,
    store_code TEXT, reason_code TEXT, details TEXT);
CREATE INDEX IF NOT EXISTS audit_ref ON audit (ref);
CREATE TABLE IF NOT EXISTS stock_holds (
    bill_id TEXT NOT NULL, inventory_item_id INTEGER NOT NULL, location_id INTEGER NOT NULL, qty INTEGER NOT NULL,
    created_at TEXT NOT NULL, PRIMARY KEY (bill_id, inventory_item_id, location_id));
"""


class SqliteRepository:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self._depth = 0

    def close(self) -> None:
        self._db.close()

    # ------------------------------------------------------------ transactions
    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._db.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield
            except BaseException:
                self._depth -= 1
                if outer:
                    self._db.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._db.execute("COMMIT")

    def _exec(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._db.execute(sql, args)

    def _upsert(self, table: str, key: str, row: Dict[str, Any]) -> None:
        """Update-then-insert (portable to SQLite builds older than 3.24, which lack ON CONFLICT DO UPDATE)."""
        cols = [c for c in row if c != key]
        with self.transaction():
            cur = self._exec("UPDATE %s SET %s WHERE %s = ?" % (table, ", ".join("%s = ?" % c for c in cols), key),
                             [row[c] for c in cols] + [row[key]])
            if cur.rowcount == 0:
                self._exec("INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(row), ", ".join("?" * len(row))), list(row.values()))

    # ------------------------------------------------------------ document series
    def series_exists(self, series: str) -> bool:
        return self._exec("SELECT 1 FROM series WHERE series = ?", (series,)).fetchone() is not None

    def series_info(self, series: str) -> Dict[str, int]:
        row = self._exec("SELECT floor, last_seq FROM series WHERE series = ?", (series,)).fetchone()
        return {"floor": row["floor"], "last": row["last_seq"]} if row else {}

    def init_series(self, series: str, floor: int, at: str) -> None:
        """Start a series after `floor` (the highest number already issued, e.g. by Shopify POS before cutover)."""
        self._exec("INSERT OR IGNORE INTO series (series, last_seq, floor, created_at) VALUES (?, ?, ?, ?)", (series, floor, floor, at))

    def allocate(self, series: str, kind: str, store_code: str, ref: str, at: str) -> str:
        with self.transaction():
            row = self._exec("SELECT last_seq FROM series WHERE series = ?", (series,)).fetchone()
            if row is None:
                raise StateConflict("series %s is not initialised" % series, code="series_missing")
            seq = row["last_seq"] + 1
            number = format_number(series, seq)
            self._exec("UPDATE series SET last_seq = ? WHERE series = ?", (seq, series))
            self._exec("INSERT INTO documents (number, series, seq, kind, store_code, ref, status, issued_at) VALUES (?, ?, ?, ?, ?, ?, 'issued', ?)",
                       (number, series, seq, kind, store_code, ref, at))
            return number

    def cancel_document(self, number: str, note: str) -> None:
        self._exec("UPDATE documents SET status = 'cancelled', note = ? WHERE number = ?", (note, number))

    def documents(self, store_code: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM documents WHERE 1=1", []
        if store_code:
            sql += " AND store_code = ?"
            args.append(store_code)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [dict(r) for r in self._exec(sql + " ORDER BY series, seq", args).fetchall()]

    def series_gaps(self, series: str) -> List[int]:
        row = self._exec("SELECT last_seq, floor FROM series WHERE series = ?", (series,)).fetchone()
        if row is None:
            return []
        have = {r["seq"] for r in self._exec("SELECT seq FROM documents WHERE series = ?", (series,)).fetchall()}
        return [s for s in range(row["floor"] + 1, row["last_seq"] + 1) if s not in have]

    # ------------------------------------------------------------ bills
    def save_bill(self, bill: Bill) -> None:
        self._upsert("bills", "bill_id", {"bill_id": bill.bill_id, "store_code": bill.store_code, "business_date": bill.business_date,
                                          "status": bill.status, "invoice_no": bill.invoice_no, "payload": json.dumps(bill.to_dict()),
                                          "updated_at": bill.updated_at})

    def get_bill(self, bill_id: str) -> Optional[Bill]:
        row = self._exec("SELECT payload FROM bills WHERE bill_id = ?", (bill_id,)).fetchone()
        return Bill.from_dict(json.loads(row["payload"])) if row else None

    def get_bill_by_invoice(self, invoice_no: str) -> Optional[Bill]:
        row = self._exec("SELECT payload FROM bills WHERE invoice_no = ?", (invoice_no,)).fetchone()
        return Bill.from_dict(json.loads(row["payload"])) if row else None

    def bills(self, store_code: Optional[str] = None, business_date: Optional[str] = None, statuses: Sequence[str] = ()) -> List[Bill]:
        sql, args = "SELECT payload FROM bills WHERE 1=1", []
        if store_code:
            sql += " AND store_code = ?"
            args.append(store_code)
        if business_date:
            sql += " AND business_date = ?"
            args.append(business_date)
        if statuses:
            sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args += list(statuses)
        return [Bill.from_dict(json.loads(r["payload"])) for r in self._exec(sql + " ORDER BY rowid", args).fetchall()]

    # ------------------------------------------------------------ refunds
    def save_refund(self, refund: Refund) -> None:
        self._upsert("refunds", "refund_id", {"refund_id": refund.refund_id, "bill_id": refund.bill_id, "store_code": refund.store_code,
                                              "status": refund.status, "credit_note_no": refund.credit_note_no, "created_day": refund.created_at[:10],
                                              "payload": json.dumps(refund.to_dict()), "updated_at": refund.money_returned_at or refund.created_at})

    def get_refund(self, refund_id: str) -> Optional[Refund]:
        row = self._exec("SELECT payload FROM refunds WHERE refund_id = ?", (refund_id,)).fetchone()
        return Refund.from_dict(json.loads(row["payload"])) if row else None

    def get_refund_by_credit_note(self, credit_note_no: str) -> Optional[Refund]:
        row = self._exec("SELECT payload FROM refunds WHERE credit_note_no = ?", (credit_note_no,)).fetchone()
        return Refund.from_dict(json.loads(row["payload"])) if row else None

    def refunds(self, bill_id: Optional[str] = None, store_code: Optional[str] = None, day: Optional[str] = None,
                statuses: Sequence[str] = ()) -> List[Refund]:
        sql, args = "SELECT payload FROM refunds WHERE 1=1", []
        for col, val in (("bill_id", bill_id), ("store_code", store_code), ("created_day", day)):
            if val:
                sql += " AND %s = ?" % col
                args.append(val)
        if statuses:
            sql += " AND status IN (%s)" % ",".join("?" * len(statuses))
            args += list(statuses)
        return [Refund.from_dict(json.loads(r["payload"])) for r in self._exec(sql + " ORDER BY rowid", args).fetchall()]

    # ------------------------------------------------------------ stock holds
    # A frame being paid for (or paid but not yet in Shopify) is held here, so another till cannot sell the
    # same last unit. The hold is released once Shopify's own count reflects the sale, or the sale did not happen.
    def holds(self, keys: Sequence[Tuple[int, int]], exclude_bill: Optional[str] = None) -> Dict[Tuple[int, int], int]:
        out: Dict[Tuple[int, int], int] = {}
        for item, loc in set(keys):
            row = self._exec("SELECT COALESCE(SUM(qty), 0) AS n FROM stock_holds WHERE inventory_item_id = ? AND location_id = ? AND bill_id != ?",
                             (item, loc, exclude_bill or "")).fetchone()
            if row["n"]:
                out[(item, loc)] = row["n"]
        return out

    def hold_stock(self, bill_id: str, wanted: Dict[Tuple[int, int], int], available: Dict[Tuple[int, int], int], at: str) -> None:
        """Check and reserve in one locked transaction: two tills cannot both take the last unit."""
        with self.transaction():
            others = self.holds(list(wanted), exclude_bill=bill_id)
            short = [(k, available.get(k, 0), others.get(k, 0), q) for k, q in wanted.items() if available.get(k, 0) - others.get(k, 0) < q]
            if short:
                (item, loc), have, held, need = short[0]
                raise StateConflict("Frame (inventory item %d) at location %d: %d in Shopify, %d already being paid for on another till, %d needed. "
                                    "Nothing was charged; pick another frame or wait." % (item, loc, have, held, need), code="stock_held")
            self._exec("DELETE FROM stock_holds WHERE bill_id = ?", (bill_id,))
            for (item, loc), q in wanted.items():
                self._exec("INSERT INTO stock_holds (bill_id, inventory_item_id, location_id, qty, created_at) VALUES (?, ?, ?, ?, ?)",
                           (bill_id, item, loc, q, at))

    def release_stock(self, bill_id: str) -> int:
        return self._exec("DELETE FROM stock_holds WHERE bill_id = ?", (bill_id,)).rowcount

    def hold_list(self, location_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """Who is holding what, with the bill's status (for the stock board and the day-end check)."""
        sql = ("SELECT h.bill_id, h.inventory_item_id, h.location_id, h.qty, h.created_at, b.status, b.store_code "
               "FROM stock_holds h LEFT JOIN bills b ON b.bill_id = h.bill_id")
        args: List[Any] = []
        if location_id is not None:
            sql += " WHERE h.location_id = ?"
            args.append(location_id)
        return [dict(r) for r in self._exec(sql + " ORDER BY h.created_at", args).fetchall()]

    # ------------------------------------------------------------ till sessions
    def save_till(self, s: TillSession) -> None:
        try:
            self._upsert("till_sessions", "session_id", {"session_id": s.session_id, "store_code": s.store_code, "status": s.status,
                                                         "opened_day": s.opened_at[:10], "payload": json.dumps(s.to_dict())})
        except sqlite3.IntegrityError:
            raise StateConflict("Store %s already has an open till." % s.store_code, code="till_already_open")

    def open_till(self, store_code: str) -> Optional[TillSession]:
        row = self._exec("SELECT payload FROM till_sessions WHERE store_code = ? AND status = ?", (store_code, OPEN)).fetchone()
        return TillSession.from_dict(json.loads(row["payload"])) if row else None

    def tills(self, store_code: str, day: Optional[str] = None) -> List[TillSession]:
        sql, args = "SELECT payload FROM till_sessions WHERE store_code = ?", [store_code]
        if day:
            sql += " AND opened_day = ?"
            args.append(day)
        return [TillSession.from_dict(json.loads(r["payload"])) for r in self._exec(sql + " ORDER BY rowid", args).fetchall()]

    def till_exists(self, session_id: str) -> bool:
        return self._exec("SELECT 1 FROM till_sessions WHERE session_id = ?", (session_id,)).fetchone() is not None

    # ------------------------------------------------------------ audit
    def audit(self, at: str, actor: str, action: str, ref: Optional[str] = None, store_code: Optional[str] = None,
              reason_code: Optional[str] = None, details: Optional[Dict[str, Any]] = None) -> None:
        self._exec("INSERT INTO audit (at, actor, action, ref, store_code, reason_code, details) VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (at, actor, action, ref, store_code, reason_code, json.dumps(details or {}, default=str)))

    def audit_log(self, ref: Optional[str] = None, store_code: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        sql, args = "SELECT * FROM audit WHERE 1=1", []
        if ref:
            sql += " AND ref = ?"
            args.append(ref)
        if store_code:
            sql += " AND store_code = ?"
            args.append(store_code)
        rows = self._exec(sql + " ORDER BY id DESC LIMIT ?", args + [limit]).fetchall()
        out = []
        for r in reversed(rows):
            d = dict(r)
            d["details"] = json.loads(d["details"] or "{}")
            out.append(d)
        return out
