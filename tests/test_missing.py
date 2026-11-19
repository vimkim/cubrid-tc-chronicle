import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_chronicle import run_metadata
from test_layout import LAYOUT_TABLES, downgrade

from chronicle.cli import main
from chronicle.collection import Collector, RequestLimitError
from chronicle.storage import Index

DAYS = {n: f"2026-10-{n:02d}T10:00:00Z" for n in range(1, 8)}


class Budgeted:
    """GitHub fake: one request per call, as the real client, and a hard budget."""

    def __init__(self, budget, discovered=()):
        self.remaining, self.discovered, self.calls = budget, list(discovered), []

    def _spend(self, endpoint):
        if self.remaining <= 0:
            raise RequestLimitError(
                "GitHub request budget exhausted; synchronization is incomplete"
            )
        self.remaining -= 1
        self.calls.append(endpoint)

    def get(self, endpoint):
        self._spend(endpoint)
        run_id = int(endpoint.rsplit("/", 1)[1])
        return run_metadata(run_id, created=DAYS[run_id])

    def pages(self, endpoint, key):
        self._spend(endpoint)
        return []

    def discover(self, repository, since, until):
        self._spend("discover")
        return [copy.deepcopy(r) for r in self.discovered]


class NoStore:
    def url(self, path):
        return "http://example.test/" + path

    def fetch(self, path):
        return None, "access_denied_or_missing"


class MissingRunTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.path = Path(self._dir.name)
        self.index = Index(self.path, writable=True)
        self.addCleanup(self.index.db.close)

    def collector(self, github):
        return Collector(self.index, github, NoStore(), suites=("shell",))

    def queue(self):
        return [r[0] for r in self.index.db.execute("SELECT run_id FROM run_queue ORDER BY run_id")]

    def runs(self):
        return [r[0] for r in self.index.db.execute("SELECT run_id FROM run ORDER BY run_id")]

    def discover_all(self, budget=1):
        # Budget 1 pays for discovery only, so no run is imported.
        found = [run_metadata(n, created=DAYS[n]) for n in (3, 1, 2, 4)]
        github = Budgeted(budget, found)
        result = self.collector(github).sync(
            since="2026-10-01", until="2026-10-07", revisit_limit=0
        )
        return github, result

    def test_discovery_queues_unimported_runs_and_import_dequeues_them(self):
        _, result = self.discover_all()
        self.assertEqual(self.runs(), [])
        self.assertEqual(self.queue(), [1, 2, 3, 4])
        self.assertEqual(result["missing_remaining"], 4)
        self.collector(Budgeted(10)).sync(run_ids=[2], revisit_limit=0)
        self.assertEqual(self.queue(), [1, 3, 4])
        self.discover_all()  # idempotent, and an imported run is not queued again
        self.assertEqual(self.queue(), [1, 3, 4])

    def test_missing_drains_oldest_first_and_resumes_after_the_budget_runs_out(self):
        self.discover_all()
        # A run costs 2 requests (run + jobs), so a budget of 5 imports two runs.
        first = Budgeted(5)
        result = self.collector(first).sync(missing=True)
        self.assertEqual(self.runs(), [1, 2])
        self.assertEqual(self.queue(), [3, 4])
        self.assertEqual(result["missing_remaining"], 2)
        self.assertNotIn("discover", first.calls)
        self.assertTrue(result["failures"])
        # The exhausted run did not count as a failed attempt.
        self.assertEqual(
            self.index.db.execute("SELECT max(attempts) FROM run_queue").fetchone()[0], 0
        )
        second = Budgeted(100)
        result = self.collector(second).sync(missing=True)
        self.assertEqual((self.runs(), self.queue()), ([1, 2, 3, 4], []))
        self.assertEqual(result["missing_remaining"], 0)
        self.assertFalse(result["failures"])

    def test_missing_window_filters_the_queue_without_discovery(self):
        self.discover_all()
        github = Budgeted(100)
        result = self.collector(github).sync(missing=True, since="2026-10-02", until="2026-10-03")
        self.assertNotIn("discover", github.calls)
        self.assertEqual(self.runs(), [2, 3])
        self.assertEqual(result["missing_remaining"], 0)
        self.assertEqual(self.queue(), [1, 4])

    def test_migration_backfills_the_queue_from_discoveries(self):
        self.discover_all()
        self.collector(Budgeted(10)).sync(run_ids=[2], revisit_limit=0)
        self.index.db.execute("DROP TABLE run_queue")
        self.index.db.execute("UPDATE meta SET value='2' WHERE key='layout'")
        self.index.db.commit()
        result = self.index.migrate()
        self.assertEqual((result["migrated"], result["queued_runs"], result["runs"]), (True, 3, 0))
        self.assertEqual(self.queue(), [1, 3, 4])
        self.assertFalse(self.index.migrate()["migrated"])

    def test_downgrade_without_queue_table_migrates_from_scratch(self):
        self.discover_all()
        downgrade(self.index)
        self.assertIn("run_queue", LAYOUT_TABLES)
        self.assertEqual(self.index.migrate()["queued_runs"], 4)

    def test_since_without_until_means_until_now(self):
        github = Budgeted(100, [run_metadata(1, created=DAYS[1])])
        result = self.collector(github).sync(since="2026-10-01", revisit_limit=0)
        self.assertEqual(self.runs(), [1])
        self.assertEqual(result["runs"], 1)
        with self.assertRaises(ValueError):
            self.collector(github).sync(until="2026-10-01")

    def cli(self, *args):
        out = io.StringIO()
        other = tempfile.TemporaryDirectory()  # the fixture index holds its own writer lock
        self.addCleanup(other.cleanup)
        with (
            patch("chronicle.cli.Collector.sync", return_value={}) as sync,
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            self.assertEqual(main(["--data-dir", other.name, "sync-data", *args, "--json"]), 0)
        json.loads(out.getvalue())
        return sync.call_args.kwargs

    def test_revisit_limit_defaults_by_selection(self):
        self.assertEqual(self.cli("--run", "5")["revisit_limit"], 0)
        self.assertEqual(self.cli("--run", "5", "--revisit-limit", "7")["revisit_limit"], 7)
        self.assertEqual(self.cli("--missing")["revisit_limit"], 0)
        self.assertEqual(self.cli("--since", "2026-10-01")["revisit_limit"], 20)
        self.assertEqual(self.cli("--run", "5", "--revisit-limit", "0")["revisit_limit"], 0)
        kwargs = self.cli("--missing", "--suite", "shell")
        self.assertTrue(kwargs["missing"])
        self.assertIsNone(kwargs["since"])


if __name__ == "__main__":
    unittest.main()
