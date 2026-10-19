import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from test_chronicle import CASE, ENGINE, ReplayGitHub, ReplayStore, run_metadata, sample

from chronicle.cli import main
from chronicle.collection import CollectionError, Collector, RequestLimitError
from chronicle.parsing import parse
from chronicle.provenance import run_pull_request
from chronicle.query import HistoryQuery, Queries
from chronicle.storage import Index


def pr_run(run_id=1, number=42):
    return {
        **run_metadata(run_id),
        "display_title": f"PR #{number} · Fix something (#999)",
        "actor": {"login": "ci-trigger"},
    }


def pr_response(number=42, author="pr-author"):
    return {
        "number": number,
        "title": "Fix something",
        "html_url": f"https://github.com/CUBRID/cubrid/pull/{number}",
        "user": {"login": author, "html_url": f"https://github.com/{author}"} if author else None,
        # Today's PR head must not replace the historical tested engine revision.
        "head": {"sha": "f" * 40},
    }


class RunAssociationTests(unittest.TestCase):
    def test_only_explicit_run_associations_identify_a_pr(self):
        run = pr_run()
        self.assertEqual(run_pull_request(run)["number"], 42)
        self.assertEqual(run_pull_request(run)["association"], "run_title")
        self.assertIsNone(run_pull_request(run)["author"])
        for event in ("push", "schedule"):
            self.assertIsNone(run_pull_request({**run, "event": event}))
        self.assertEqual(run_pull_request({**run, "event": "workflow_dispatch"})["number"], 42)
        self.assertIsNone(run_pull_request({**run, "display_title": "Fix something (#42)"}))
        self.assertIsNone(run_pull_request({**run, "name": "PR #43 · Conflicting title"}))
        self.assertIsNone(run_pull_request(run_metadata(1)))
        self.assertIsNone(run_pull_request(None))

    def test_pull_request_events_require_one_explicit_association(self):
        run = {**run_metadata(1), "event": "pull_request", "pull_requests": [{"number": 42}]}
        self.assertEqual(run_pull_request(run)["number"], 42)
        run["pull_requests"].append({"number": 43})
        self.assertIsNone(run_pull_request(run))


class PullRequestHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.index = Index(self.root, writable=True)
        self.queries = Queries(self.index)

    def tearDown(self):
        self.index.__exit__()
        self.temporary.cleanup()

    def retain(self, run):
        self.index.record_run(run, "2030-01-01T00:00:00Z")
        return self.index.ingest(parse(sample(run["id"])))

    def test_metadata_refresh_enriches_existing_snapshots_and_stays_offline_on_queries(self):
        first = self.retain(pr_run())
        self.retain(pr_run(2))
        self.retain(run_metadata(3))
        bundle = self.index.bundle(first["execution_id"])
        github = Mock()
        github.get.return_value = pr_response()
        store = Mock()
        store.fetch.side_effect = AssertionError("result download")
        result = Collector(self.index, github, store).refresh_pr_metadata()
        self.assertEqual(result, {"runs": 3, "linked_runs": 2, "failures": []})
        github.get.assert_called_once_with("repos/CUBRID/cubrid/pulls/42")
        github.discover.assert_not_called()
        github.pages.assert_not_called()
        self.assertEqual(self.index.bundle(first["execution_id"]), bundle)
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM observation").fetchone()[0], 3)
        self.assertEqual(
            {r[0] for r in self.index.db.execute("SELECT revisit_at FROM run")},
            {"2030-01-01T00:00:00Z"},
        )
        with patch("chronicle.collection.GitHub.get", side_effect=AssertionError("network")):
            report = self.queries.history(HistoryQuery(CASE, pr=42))
        self.assertEqual(len(report["occurrences"]), 2)
        self.assertIn("historical_pr_unknown", report["reasons"])
        pr = report["target"]["pull_request"]
        self.assertEqual(pr["author"], "pr-author")
        self.assertEqual(pr["url"], "https://github.com/CUBRID/cubrid/pull/42")
        self.assertEqual(json.loads(self.index.archive.read(pr["metadata_digest"])), pr_response())
        self.assertEqual(report["target"]["engine_sha"], ENGINE)

        for args in (
            ["history", CASE, "--pr", "42"],
            ["history", CASE, "--pr", "42", "--commits"],
            ["explain", "--case", CASE, "--run", "1"],
        ):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["--data-dir", str(self.root), *args]), 0)
            self.assertIn("PR #42; author @pr-author", out.getvalue())

    def test_normal_sync_enriches_old_history_without_duplicate_observations(self):
        run = pr_run()
        jobs = [
            {
                "name": "shard sql 00",
                "run_attempt": 1,
                "id": 10,
                "status": "completed",
                "conclusion": "failure",
                "completed_at": "2026-10-15T12:00:00Z",
            }
        ]
        github = ReplayGitHub(run, jobs)
        get_run = github.get
        github.get = Mock(side_effect=lambda e: pr_response() if "/pulls/" in e else get_run(e))
        collector = Collector(self.index, github, ReplayStore(sample()), suites=("sql",))
        self.assertEqual(collector.sync(run_ids=[1], revisit_limit=0)["inserted_snapshots"], 1)
        self.assertEqual(collector.sync(run_ids=[1], revisit_limit=0)["inserted_snapshots"], 0)
        target = self.queries.history(HistoryQuery(CASE))["target"]
        self.assertEqual(target["pull_request"]["author"], "pr-author")
        self.assertEqual(target["engine_sha"], ENGINE)

        def failed_lookup(endpoint):
            if "/pulls/" in endpoint:
                raise CollectionError("temporarily unavailable")
            return get_run(endpoint)

        github.get.side_effect = failed_lookup
        result = collector.sync(run_ids=[1], revisit_limit=0)
        self.assertTrue(result["failures"])
        self.assertEqual(result["inserted_snapshots"], 0)
        target = self.queries.history(HistoryQuery(CASE))["target"]
        self.assertEqual(target["pull_request"]["author"], "pr-author")
        self.assertIn("error", target["pull_request"])

    def test_failed_pr_lookup_preserves_testcase_evidence(self):
        github = ReplayGitHub(
            pr_run(),
            [
                {
                    "name": "shard sql 00",
                    "run_attempt": 1,
                    "id": 10,
                    "status": "completed",
                    "conclusion": "failure",
                    "completed_at": "2026-10-15T12:00:00Z",
                }
            ],
        )
        get_run = github.get

        def get(endpoint):
            if "/pulls/" in endpoint:
                raise CollectionError("PR unavailable")
            return get_run(endpoint)

        github.get = get
        result = Collector(self.index, github, ReplayStore(sample()), suites=("sql",)).sync(
            run_ids=[1], revisit_limit=0
        )
        self.assertEqual(result["inserted_snapshots"], 1)
        self.assertIn("PR #42 metadata", result["failures"][0]["reason"])
        pr = self.queries.history(HistoryQuery(CASE))["target"]["pull_request"]
        self.assertEqual(pr["number"], 42)
        self.assertIsNone(pr["author"])

    def test_refresh_failure_keeps_retained_author_and_retry_clears_error(self):
        self.retain(pr_run())
        github = Mock()
        github.get.return_value = pr_response()
        collector = Collector(self.index, github, Mock())
        collector.refresh_pr_metadata()
        github.get.side_effect = CollectionError("temporarily unavailable")
        self.assertTrue(collector.refresh_pr_metadata()["failures"])
        pr = self.queries.history(HistoryQuery(CASE))["target"]["pull_request"]
        self.assertEqual(pr["author"], "pr-author")
        self.assertIn("error", pr)
        github.get.side_effect = None
        self.assertFalse(collector.refresh_pr_metadata()["failures"])
        self.assertNotIn(
            "error", self.queries.history(HistoryQuery(CASE))["target"]["pull_request"]
        )

    def test_request_limit_stops_refresh_and_missing_run_is_reported(self):
        self.retain(pr_run())
        self.retain(pr_run(2, 43))
        github = Mock()
        github.get.side_effect = RequestLimitError("budget exhausted")
        result = Collector(self.index, github, Mock()).refresh_pr_metadata(run_ids=[99, 1, 2])
        self.assertEqual(result["runs"], 1)
        self.assertEqual(len(result["failures"]), 2)
        github.get.assert_called_once()

    def test_deleted_author_is_unknown_and_unlinked_runs_never_use_testcase_branch(self):
        self.retain(pr_run())
        self.retain(run_metadata(2))
        github = Mock()
        github.get.return_value = pr_response(author=None)
        Collector(self.index, github, Mock()).refresh_pr_metadata()
        report = self.queries.history(HistoryQuery(CASE))
        self.assertIsNone(report["occurrences"][0]["pull_request"]["author"])
        self.assertIsNone(report["occurrences"][1]["pull_request"])
        self.assertEqual(report["occurrences"][1]["testcase_branch"], "tc/pr-42")

    def test_metadata_only_cli_does_not_discover_or_fetch_results(self):
        self.retain(pr_run())
        # Close the writer so the CLI can take its lock.
        self.index.__exit__()
        try:
            out = io.StringIO()
            with (
                patch("chronicle.collection.GitHub.get", return_value=copy.deepcopy(pr_response())),
                patch(
                    "chronicle.collection.GitHub.discover", side_effect=AssertionError("discover")
                ),
                patch(
                    "chronicle.collection.ResultStore.fetch", side_effect=AssertionError("fetch")
                ),
                contextlib.redirect_stdout(out),
            ):
                self.assertEqual(
                    main(["--data-dir", str(self.root), "sync", "--pr-metadata-only"]), 0
                )
            self.assertIn("1 runs linked to a PR", out.getvalue())
        finally:
            self.index = Index(self.root, writable=True)


if __name__ == "__main__":
    unittest.main()
