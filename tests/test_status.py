import contextlib
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from test_chronicle import CASE, ENGINE, run_metadata, sample

from chronicle.cli import main
from chronicle.parsing import parse
from chronicle.query import AmbiguousIdentity, Queries, StatusQuery
from chronicle.storage import Index

NOW = datetime(2026, 10, 15, 16, tzinfo=UTC)


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.index = Index(self.path, writable=True)
        self.addCleanup(self.index.__exit__)
        self.queries = Queries(self.index)

    def ingest(self, run_id=1, outcome="FAIL", *, metadata=None, **kwargs):
        bundle = sample(run_id, outcome, **kwargs)
        bundle.metadata.update(metadata or {})
        self.index.record_run(run_metadata(run_id), "2026-10-17T00:00:00Z")
        return self.index.ingest(parse(bundle))

    def status(self, case=CASE, *, now=NOW, **kwargs):
        return self.queries.status(StatusQuery(case, **kwargs), now=now)

    def discover(self, until=NOW):
        self.index.record_discovery(
            "CUBRID/cubrid",
            "2026-10-01T00:00:00Z",
            until.strftime("%Y-%m-%dT%H:%M:%SZ"),
            [run_metadata(row[0]) for row in self.index.db.execute("SELECT run_id FROM run")],
            True,
        )

    def test_fail_then_pass_reports_latest_pass_without_losing_failure(self):
        self.ingest(1, "FAIL", complete_context=True)
        self.ingest(2, "PASS", at="2026-10-15T13:00:00Z", complete_context=True)
        self.discover()
        report = self.status(variant="default")
        self.assertEqual(report["status"], "PASSING")
        self.assertEqual(report["latest_known"][0]["run_id"], 2)
        self.assertEqual(report["counts"]["FAIL"], 1)
        self.assertEqual(report["flakiness"]["assessment"], "SUSPECTED")
        pair = report["flakiness"]["examples"][0]
        self.assertEqual((pair["pass"]["run_id"], pair["failure"]["run_id"]), (2, 1))
        evidence = pair["failure"]
        self.assertEqual(len(evidence["evidence_digest"]), 64)
        self.assertIn(evidence["evidence"], self.index.bundle(evidence["execution_id"]).files)
        self.assertEqual(report["freshness"]["state"], "FRESH")
        self.assertTrue(report["coverage"]["complete"])
        self.assertFalse(report["freshness"]["needs_refresh"])

    def test_pass_only_and_repeated_failure_are_useful_without_a_failure_target(self):
        self.ingest(1, "PASS", complete_context=True)
        self.assertEqual(self.status()["status"], "PASSING")
        self.assertEqual(self.status()["flakiness"]["assessment"], "UNKNOWN")
        self.ingest(2, at="2026-10-15T13:00:00Z", complete_context=True)
        self.ingest(3, at="2026-10-15T14:00:00Z", complete_context=True)
        self.assertEqual(self.status()["status"], "FAILING")
        self.assertEqual(self.status()["flakiness"]["assessment"], "SUSPECTED")

    def test_repeated_consistent_context_does_not_establish_flakiness(self):
        self.ingest(1, complete_context=True)
        self.ingest(2, complete_context=True)
        report = self.status()
        self.assertEqual(report["status"], "FAILING")
        self.assertEqual(report["flakiness"]["assessment"], "NOT_OBSERVED")

    def test_mixed_outcomes_across_engine_or_testcase_revisions_are_not_flaky_evidence(self):
        self.ingest(1, complete_context=True)
        self.ingest(2, "PASS", engine="e" * 40, complete_context=True)
        self.ingest(3, "PASS", tc="f" * 40, complete_context=True)
        report = self.status()
        self.assertEqual(report["flakiness"]["assessment"], "UNKNOWN")
        self.assertTrue(report["flakiness"]["mixed_outcomes"])
        self.assertEqual(report["flakiness"]["examples"], [])

    def test_missing_comparison_context_never_establishes_flakiness(self):
        self.ingest(1)
        self.ingest(2, "PASS")
        report = self.status()
        self.assertEqual(report["flakiness"]["assessment"], "UNKNOWN")
        self.assertIn("comparison_context_unknown", report["flakiness"]["reasons"])

    def test_equal_completion_times_remain_mixed(self):
        self.ingest(1)
        self.ingest(2, "PASS")
        report = self.status(limit=1)
        self.assertEqual(report["status"], "MIXED")
        self.assertEqual(report["latest_known_count"], 2)
        self.assertEqual(len(report["latest_known"]), 1)

    def test_error_counts_as_failing_and_skipped_latest_remains_unknown(self):
        for run_id, outcome, expected in ((1, "ERROR", "FAILING"), (2, "SKIP", "UNKNOWN")):
            parsed = parse(sample(run_id, at=f"2026-10-15T1{run_id}:00:00Z"))
            parsed.observations = [replace(parsed.observations[0], outcome=outcome)]
            self.index.ingest(parsed)
            self.index.record_run(run_metadata(run_id), "2026-10-17T00:00:00Z")
            report = self.status()
            self.assertEqual(report["status"], expected)
            self.assertEqual(report["counts"][outcome], 1)

    def test_mutated_result_snapshots_for_one_job_are_not_independent(self):
        self.ingest(1, complete_context=True)
        self.ingest(1, "PASS", complete_context=True)
        self.assertEqual(self.status()["flakiness"]["assessment"], "UNKNOWN")

    def test_verified_retry_can_support_suspected_flakiness(self):
        self.ingest(1, complete_context=True)
        self.ingest(
            1,
            "PASS",
            complete_context=True,
            metadata={"execution_attempt": 2, "execution_job_id": 11},
        )
        self.assertEqual(self.status()["flakiness"]["assessment"], "SUSPECTED")

    def test_unordered_verdict_prevents_claiming_a_latest_status(self):
        self.ingest(1, "PASS")
        self.ingest(2, at=None)
        report = self.status()
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertEqual(report["unordered_observations"], 1)
        self.assertIn("ordering_unknown", report["reasons"])

    def test_freshness_checks_discovery_and_case_age_not_global_sync_stamp(self):
        self.ingest()
        self.index.db.execute("INSERT INTO meta VALUES ('last_sync',?)", (NOW.isoformat(),))
        report = self.status()
        self.assertEqual(report["freshness"]["state"], "UNKNOWN")
        self.discover(until=NOW - timedelta(days=2))
        self.assertEqual(self.status()["freshness"]["state"], "STALE")
        self.discover()
        self.assertEqual(self.status(max_age_hours=1)["freshness"]["observation_state"], "STALE")
        self.assertEqual(self.status(max_age_hours=1)["freshness"]["discovery_state"], "FRESH")

    def test_absent_identity_and_old_observations_are_not_passing(self):
        self.assertEqual(self.status("absent")["status"], "NOT_INDEXED")
        self.ingest()
        report = self.status(now=NOW + timedelta(days=15))
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertEqual(report["observations"], 0)

    def test_future_discovery_window_cannot_keep_an_old_scan_fresh(self):
        self.ingest()
        with patch("chronicle.storage.now", return_value=NOW.strftime("%Y-%m-%dT%H:%M:%SZ")):
            self.discover(until=NOW + timedelta(days=10))
        report = self.status(now=NOW + timedelta(days=2))
        self.assertEqual(report["freshness"]["discovery_state"], "STALE")
        self.assertEqual(report["freshness"]["discovery_age_hours"], 48)

    def test_branch_engine_pr_and_window_filters_use_recorded_evidence(self):
        self.ingest(1, complete_context=True)
        self.ingest(
            2,
            "PASS",
            at="2026-10-15T13:00:00Z",
            engine="e" * 40,
            complete_context=True,
            metadata={"engine_branch": "other"},
        )
        run = run_metadata(2)
        run.update(name="PR #42 · CI", chronicle_pr={"number": 42, "author": "alice"})
        self.index.record_run(run, "2026-10-17T00:00:00Z")
        self.assertEqual(self.status(branch="topic")["status"], "FAILING")
        self.assertEqual(self.status(engine_sha=ENGINE[:12])["status"], "FAILING")
        report = self.status(pr=42)
        self.assertEqual(report["observations"], 1)
        self.assertEqual(report["latest_known"][0]["pull_request"]["author"], "alice")
        # Run 1 has no known PR and could hide a later outcome in that scope.
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("historical_pr_unknown", report["reasons"])
        self.assertEqual(self.status(since="2026-10-16", until="2026-10-16")["observations"], 0)

    def test_missing_run_creation_is_unknown_not_silently_excluded(self):
        self.ingest()
        self.index.db.execute("DELETE FROM run")
        report = self.status()
        self.assertEqual(report["status"], "UNKNOWN")
        self.assertIn("run_creation_unknown", report["reasons"])

    def test_names_and_variants_require_disambiguation(self):
        self.ingest()
        self.ingest(suite="medium")
        with self.assertRaises(AmbiguousIdentity):
            self.status("shared.sql")
        self.assertEqual(self.status("shared.sql", suite="sql")["status"], "FAILING")
        self.ingest(2, complete_context=True)
        with self.assertRaises(AmbiguousIdentity):
            self.status()
        self.assertEqual(self.status(variant="unknown")["observations"], 1)

    def test_limits_bound_evidence_without_changing_assessment_or_counts(self):
        for run_id in range(1, 8):
            self.ingest(run_id, complete_context=True)
        report = self.status(limit=1)
        self.assertEqual(report["counts"]["FAIL"], 7)
        self.assertEqual(report["latest_known_count"], 7)
        self.assertEqual(len(report["recent"]), 1)
        self.assertTrue(report["recent_truncated"])
        for options in (
            {"days": 0},
            {"limit": 101},
            {"max_age_hours": 0},
            {"days": 2, "since": "2026-10-15"},
            {"since": "2026-10-16", "until": "2026-10-15"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.status(**options)

    def test_cli_is_offline_json_is_clean_and_exit_codes_describe_query_success(self):
        self.ingest()
        for options, code, result in (
            (["status", CASE, "--since", "2026-10-15", "--until", "2026-10-15"], 0, "FAILING"),
            (["status", "absent"], 0, "NOT_INDEXED"),
            (["status", CASE, "--limit", "0"], 2, "operational_error"),
            (["status", CASE, "--days", "999999999999999999"], 2, "operational_error"),
        ):
            out, err = io.StringIO(), io.StringIO()
            with (
                patch("socket.socket", side_effect=AssertionError("network")),
                patch("subprocess.run", side_effect=AssertionError("subprocess")),
                contextlib.redirect_stdout(out),
                contextlib.redirect_stderr(err),
            ):
                actual = main(["--data-dir", str(self.path), "--json", *options])
            self.assertEqual(actual, code, err.getvalue())
            report = json.loads(out.getvalue() or err.getvalue())
            self.assertEqual(report.get("status") or report.get("error"), result)
            self.assertEqual(report["schema_version"], 1)

    def test_cli_ambiguity_and_subcommand_json(self):
        self.ingest()
        self.ingest(suite="medium")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["--data-dir", str(self.path), "status", "shared.sql", "--json"])
        self.assertEqual(code, 3)
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(len(json.loads(err.getvalue())["candidates"]), 2)

    def test_collection_command_and_legacy_alias_preserve_json_and_scope(self):
        result = {"runs": 0, "failures": []}
        for command in ("sync-data", "sync"):
            with tempfile.TemporaryDirectory() as directory:
                out, err = io.StringIO(), io.StringIO()
                with (
                    patch("chronicle.cli.Collector.sync", return_value=result) as collect,
                    contextlib.redirect_stdout(out),
                    contextlib.redirect_stderr(err),
                ):
                    code = main(["--data-dir", directory, command, "--run", "42", "--json"])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(out.getvalue())["runs"], 0)
                collect.assert_called_once_with(
                    run_ids=[42], since=None, until=None, revisit_limit=20
                )
                self.assertEqual("legacy alias" in err.getvalue(), command == "sync")


if __name__ == "__main__":
    unittest.main()
