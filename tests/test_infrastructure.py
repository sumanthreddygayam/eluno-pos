"""HTTP retry semantics and the SQLite repository (gapless numbering under concurrency, atomicity)."""
import json
import os
import tempfile
import threading
import unittest

from eluno_pos.adapters.http import HttpClient, RetryPolicy, TransportError
from eluno_pos.adapters.sqlite_repo import SqliteRepository
from eluno_pos.domain.errors import StateConflict
from eluno_pos.domain.till import TillSession
from eluno_pos.ports import AmbiguousWrite, IntegrationError, UpstreamRejected


class Script:
    """A transport that plays back responses (status, headers, body) or raises TransportErrors."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        status, hdrs, payload = step
        return status, hdrs, json.dumps(payload).encode()


def client(script):
    sleeps = []
    c = HttpClient("http://x", "test", transport=script, policy=RetryPolicy(max_attempts=4), sleep=sleeps.append)
    return c, sleeps


OK = (201, {}, {"ok": True})


class RetrySemantics(unittest.TestCase):
    def test_429_honours_retry_after_even_for_writes(self):
        c, sleeps = client(Script((429, {"retry-after": "1.0"}, {"errors": "slow down"}), OK))
        self.assertEqual(c.post("orders.json", {}, idempotent=False), {"ok": True})
        self.assertEqual(sleeps, [1.0])

    def test_503_retried_only_when_idempotent(self):
        c, _ = client(Script((503, {}, {}), OK))
        self.assertEqual(c.post("terminal/charges", {}, idempotent=True), {"ok": True})
        c, _ = client(Script((503, {}, {}), OK))
        with self.assertRaises(AmbiguousWrite):
            c.post("orders.json", {}, idempotent=False)

    def test_lost_response_on_a_write_is_ambiguous(self):
        c, _ = client(Script(TransportError("timed out", sent=True)))
        with self.assertRaises(AmbiguousWrite):
            c.post("orders.json", {}, idempotent=False)

    def test_connection_refused_is_safe_to_retry(self):
        c, _ = client(Script(TransportError("refused", sent=False), OK))
        self.assertEqual(c.post("orders.json", {}, idempotent=False), {"ok": True})

    def test_4xx_is_not_retried(self):
        s = Script((422, {}, {"errors": {"price": ["must be a decimal string"]}}), OK)
        c, _ = client(s)
        with self.assertRaises(UpstreamRejected) as e:
            c.post("orders.json", {}, idempotent=False)
        self.assertIn("decimal string", str(e.exception))
        self.assertEqual(len(s.calls), 1)

    def test_gives_up_after_the_policy(self):
        c, sleeps = client(Script(*[(503, {}, {})] * 4))
        with self.assertRaises(IntegrationError):
            c.get("orders.json")
        self.assertEqual(len(sleeps), 3)

    def test_follows_pagination(self):
        s = Script((200, {"link": '<http://x/orders.json?page_info=2>; rel="next"'}, {"orders": [1, 2]}), (200, {}, {"orders": [3]}))
        c, _ = client(s)
        self.assertEqual(c.get_all("orders.json", {"limit": 2}, "orders"), [1, 2, 3])
        self.assertEqual(s.calls[1][1], "http://x/orders.json?page_info=2")


class Repository(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.repo = SqliteRepository(os.path.join(self.dir, "pos.db"))
        self.repo.init_series("ELU/HSR/FY26/", 2, "t")

    def tearDown(self):
        self.repo.close()

    def test_series_continues_after_the_floor_and_has_no_gaps_under_concurrency(self):
        numbers, errors = [], []

        def worker():
            try:
                for _ in range(25):
                    numbers.append(self.repo.allocate("ELU/HSR/FY26/", "invoice", "HSR", "ref", "t"))
            except Exception as e:  # pragma: no cover
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(numbers), ["ELU/HSR/FY26/%05d" % i for i in range(3, 203)])
        self.assertEqual(self.repo.series_gaps("ELU/HSR/FY26/"), [])

    def test_a_failed_transaction_does_not_burn_a_number(self):
        with self.assertRaises(RuntimeError):
            with self.repo.transaction():
                self.repo.allocate("ELU/HSR/FY26/", "invoice", "HSR", "ref", "t")
                raise RuntimeError("Shopify-independent failure after allocation")
        self.assertEqual(self.repo.allocate("ELU/HSR/FY26/", "invoice", "HSR", "ref", "t"), "ELU/HSR/FY26/00003")

    def test_unknown_series_is_refused(self):
        with self.assertRaises(StateConflict):
            self.repo.allocate("ELU/XX/FY26/", "invoice", "XX", "ref", "t")

    def test_two_tills_racing_for_the_last_unit(self):
        """Both tills read '1 in Shopify' at the same instant; only one can hold it."""
        key, results = (92000005, 70000002), []

        def till(bill_id):
            try:
                self.repo.hold_stock(bill_id, {key: 1}, {key: 1}, "t")
                results.append(("held", bill_id))
            except StateConflict as e:
                results.append((e.code, bill_id))

        threads = [threading.Thread(target=till, args=("bill_%d" % i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(r for r, _ in results), ["held"] + ["stock_held"] * 5)
        self.assertEqual(self.repo.holds([key]), {key: 1})
        winner = next(b for r, b in results if r == "held")
        self.assertEqual(self.repo.holds([key], exclude_bill=winner), {})  # its own hold is not counted against it
        self.repo.release_stock(winner)
        self.assertEqual(self.repo.holds([key]), {})

    def test_one_open_till_per_store(self):
        self.repo.save_till(TillSession("T1", "HSR", 1, "2026-10-22T10:00:00+05:30", "S108", 0))
        with self.assertRaises(StateConflict):
            self.repo.save_till(TillSession("T2", "HSR", 1, "2026-10-22T11:00:00+05:30", "S108", 0))
        self.repo.save_till(TillSession("T3", "JN", 5, "2026-10-22T10:00:00+05:30", "S120", 0))


if __name__ == "__main__":
    unittest.main()
