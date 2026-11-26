import contextlib
import io
import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from test_chronicle import CASE, run_metadata, sample

from chronicle.cli import main
from chronicle.parsing import parse
from chronicle.query import Queries, StatusQuery
from chronicle.storage import Index

NOW = datetime(2026, 10, 15, 16, tzinfo=UTC)
WINDOW = ("--since", "2026-10-15", "--until", "2026-10-15")


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.index = Index(self.path, writable=True)
        self.addCleanup(self.index.__exit__)

    def ingest(self, run_id=1, outcome="FAIL", **kwargs):
        self.index.record_run(run_metadata(run_id), "2026-10-17T00:00:00Z")
        return self.index.ingest(parse(sample(run_id, outcome, **kwargs)))

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(self.path), *argv])
        return code, out.getvalue(), err.getvalue()


class SuiteTests(Fixture):
    def test_ci_names_are_aliases(self):
        self.ingest(complete_context=True)
        for suite in ("sql", "test_sql"):
            code, out, _ = self.cli("status", CASE, "--suite", suite, *WINDOW, "--json")
            self.assertEqual((code, json.loads(out)["status"]), (0, "FAILING"))
        code, out, _ = self.cli("find", "shared", "--suite", "test_medium", "--json")
        self.assertEqual((code, json.loads(out)["candidates"]), (0, []))

    def test_uncollected_suite_answers_unsupported_for_every_query_command(self):
        for suite in ("sql_by_cci", "test_sql_by_cci"):
            for argv, key in (
                (["status", CASE], "status"),
                (["find", CASE], "status"),
                (["coverage"], "status"),
                (["history", CASE], "history_result"),
                (["explain", "--case", CASE, "--run", "1"], "history_result"),
            ):
                code, out, err = self.cli(*argv, "--suite", suite, "--json")
                report = json.loads(out)
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(report[key], "UNSUPPORTED_SUITE")
                self.assertEqual(report["reasons"], ["suite_not_collected"])

    def test_sync_refuses_uncollected_suite(self):
        code, out, _ = self.cli("sync-data", "--run", "1", "--suite", "test_sql_by_cci", "--json")
        self.assertEqual((code, json.loads(out)["error"]), (2, "unsupported_suite"))
        code, out, err = self.cli("sync-data", "--run", "1", "--suite", "sql_by_cci")
        self.assertEqual((code, out), (2, ""))
        self.assertIn("not collected", err)


class JsonContractTests(Fixture):
    def test_usage_errors_are_one_json_document_on_stdout(self):
        for argv in (["status", "--suite", "bogus", "x", "--json"], ["--json", "nope"]):
            code, out, err = self.cli(*argv)
            self.assertEqual((code, err), (2, ""))
            report = json.loads(out)
            self.assertEqual(report["error"], "usage_error")
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as raised:
            main(["status", "--suite", "bogus", "x"])
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid suite", err.getvalue())

    def test_errors_and_missing_index_use_stdout_under_json(self):
        code, out, err = main_with(Path(self.temporary.name) / "none", "status", "x", "--json")
        self.assertEqual((code, err), (2, ""))
        self.assertEqual(json.loads(out)["error"], "operational_error")

    def test_ambiguity_is_capped_on_stdout_and_text_stays_on_stderr(self):
        self.index.db.executemany(
            "INSERT INTO testcase(repository,suite,path,variant) VALUES (?,?,?,'')",
            [("CUBRID/cubrid", "sql", f"sql/many/case_{i}.sql") for i in range(25)],
        )
        self.index.db.commit()
        code, out, err = self.cli("find", "many", "--json")
        total = len(json.loads(out)["candidates"])
        self.assertEqual(total, 25)
        code, out, err = self.cli("status", "many", "--json")
        report = json.loads(out)
        self.assertEqual((code, err), (3, ""))
        self.assertEqual((len(report["candidates"]), report["candidate_count"]), (20, total))
        code, out, _ = self.cli("history", "many", "--limit", "2", "--json")
        self.assertEqual(len(json.loads(out)["candidates"]), 2)
        code, out, err = self.cli("status", "many")
        self.assertEqual((code, out), (3, ""))
        self.assertIn("more; narrow", err)


def main_with(path, *argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(["--data-dir", str(path), *argv])
    return code, out.getvalue(), err.getvalue()


class AbsenceTests(Fixture):
    def status(self, case, **kwargs):
        return Queries(self.index).status(StatusQuery(case, **kwargs), now=NOW)

    def test_failure_only_evidence_cannot_prove_absence(self):
        self.ingest()
        self.index.db.execute("UPDATE execution SET complete=0")  # failure lists only
        report = self.status("never_seen.sql", suite="sql")
        self.assertEqual(report["status"], "NOT_INDEXED")
        self.assertEqual(report["not_indexed_reason"], "suite_failures_only")
        self.assertTrue(report["suite_last_sync"])

    def test_never_observed_with_complete_evidence(self):
        self.ingest(complete_context=True)
        report = self.status("never_seen.sql", suite="sql")
        self.assertEqual(report["not_indexed_reason"], "never_observed")
        self.assertIsNone(self.status(CASE, suite="sql")["not_indexed_reason"])

    def test_observed_outside_window(self):
        self.ingest(complete_context=True)
        report = self.status(CASE, suite="sql", since="2026-10-14", until="2026-10-14T01:00:00Z")
        self.assertEqual(report["not_indexed_reason"], "observed_outside_window")
        self.assertTrue(report["suite_last_sync"])

    def test_suite_last_sync_is_the_newest_completion_not_the_last_revisit(self):
        self.ingest()
        self.index.db.execute(
            "UPDATE execution SET completed_at='2026-10-01T00:00:00Z',"
            "last_seen='2026-11-26T00:00:00Z'"
        )
        report = self.status("never_seen.sql", suite="sql")
        self.assertEqual(report["suite_last_sync"], "2026-10-01T00:00:00Z")
        self.assertEqual(report["suite_last_checked"], "2026-11-26T00:00:00Z")

    def test_history_reports_the_reason_too(self):
        from chronicle.query import HistoryQuery

        self.ingest(complete_context=True)
        report = Queries(self.index).history(HistoryQuery("never_seen.sql", suite="sql"))
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")
        self.assertEqual(report["not_indexed_reason"], "never_observed")
        self.assertIsNone(Queries(self.index).history(HistoryQuery(CASE))["not_indexed_reason"])


if __name__ == "__main__":
    unittest.main()
