import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from test_chronicle import CASE, ENGINE, TC, run_metadata, sample

from chronicle.cli import main
from chronicle.parsing import parse
from chronicle.storage import Index


class FailuresTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.index = Index(self.path, writable=True)
        self.addCleanup(self.index.__exit__)

    def ingest_pr(
        self,
        run_id,
        pr_number,
        outcome="FAIL",
        *,
        suite="sql",
        at="2026-10-15T12:00:00Z",
        engine=ENGINE,
        tc=TC,
    ):
        meta = run_metadata(run_id)
        meta["event"] = "issue_comment"
        meta["name"] = f"PR #{pr_number} · [CBRD-1] title"
        meta["chronicle_pr"] = {
            "number": pr_number,
            "url": f"https://github.com/CUBRID/cubrid/pull/{pr_number}",
            "title": "[CBRD-1] title",
            "base_ref": "develop",
            "head_ref": f"CBRD-{pr_number}",
            "author": "vimkim",
            "author_url": "https://github.com/vimkim",
            "association": "run_title",
        }
        self.index.record_run(meta, "2026-10-17T00:00:00Z")
        return self.index.ingest(
            parse(sample(run_id, outcome, suite=suite, at=at, engine=engine, tc=tc))
        )

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(self.path), *argv])
        return code, out.getvalue(), err.getvalue()

    def test_pr_latest_run_and_testcase_sha_fields(self):
        # PR 42 fails in run 100 (sql) and passes in run 101 (sql), still fails in run 101 (medium).
        self.ingest_pr(100, 42, "FAIL", suite="sql", at="2026-10-15T10:00:00Z")
        self.ingest_pr(101, 42, "PASS", suite="sql", at="2026-10-15T12:00:00Z")
        self.ingest_pr(101, 42, "FAIL", suite="medium", at="2026-10-15T12:00:00Z")

        code, out, err = self.cli("failures", "--pr", "42", "--json")
        self.assertEqual((code, err), (0, ""))
        doc = json.loads(out)
        self.assertEqual(doc["schema_version"], 1)
        self.assertEqual(doc["count"], 2)
        by_run_suite = {(item["run_id"], item["suite"]): item for item in doc["items"]}
        self.assertFalse(by_run_suite[(100, "sql")]["is_latest_pr_suite_run"])
        self.assertTrue(by_run_suite[(101, "medium")]["is_latest_pr_suite_run"])
        self.assertEqual(by_run_suite[(101, "medium")]["testcase_sha"], TC)
        self.assertEqual(by_run_suite[(101, "medium")]["testcase_branch"], "tc/pr-42")
        self.assertEqual(doc["latest_runs"]["sql"]["run_id"], 101)
        self.assertEqual(doc["latest_runs"]["medium"]["run_id"], 101)

        # With --latest-run, superseded sql failure in run 100 is filtered out.
        code, out, err = self.cli("failures", "--pr", "42", "--latest-run", "--json")
        self.assertEqual((code, err), (0, ""))
        latest = json.loads(out)
        self.assertEqual(latest["count"], 1)
        self.assertEqual(latest["items"][0]["suite"], "medium")
        self.assertEqual(latest["items"][0]["run_id"], 101)

    def test_filters_run_case_days_and_empty(self):
        recent = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.ingest_pr(200, 77, "FAIL", suite="sql", at=recent)
        self.ingest_pr(201, 78, "FAIL", suite="medium", at="2026-10-01T00:00:00Z")

        code, out, _ = self.cli("failures", "--run", "200", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["count"], 1)

        code, out, _ = self.cli("failures", "--case", "shared", "--suite", "test_sql", "--json")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc["count"], 1)
        self.assertEqual(doc["items"][0]["testcase"]["path"], CASE)

        code, out, _ = self.cli("failures", "--days", "7", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["count"], 1)
        self.assertEqual(json.loads(out)["items"][0]["run_id"], 200)

        code, out, _ = self.cli("failures", "--pr", "999999", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["count"], 0)
        self.assertEqual(json.loads(out)["items"], [])

    def test_unsupported_suite_and_human_output(self):
        code, out, err = self.cli("failures", "--suite", "sql_by_cci", "--json")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["status"], "UNSUPPORTED_SUITE")

        self.ingest_pr(300, 88, "FAIL", suite="sql")
        code, out, err = self.cli("failures", "--pr", "88")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Failures: 1 item(s)", out)
        self.assertIn("PR #88", out)


if __name__ == "__main__":
    unittest.main()
