import contextlib
import copy
import gzip
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

from chronicle.cli import main, read_bundle
from chronicle.collection import CollectionError, Collector, GitHub, RequestLimitError, timestamp
from chronicle.models import PARSER_VERSION, Bundle, Observation
from chronicle.parsing import canonical_path, parse
from chronicle.query import AmbiguousIdentity, HistoryQuery, Queries
from chronicle.storage import Index

FIXTURES = Path(__file__).parent / "fixtures"
CASE = "sql/_fixture/cases/shared.sql"
ENGINE = "a" * 40
TC = "b" * 40


def sample(
    run_id=1,
    outcome="FAIL",
    *,
    suite="sql",
    at="2026-10-15T12:00:00Z",
    engine=ENGINE,
    tc=TC,
    complete_context=False,
):
    case = CASE.replace("sql/", suite + "/", 1)
    verdict = "nok" if outcome == "FAIL" else "ok"
    files = {
        "build.read": f"sha={engine}\nmode=debug\nrun_attempt=1\n".encode(),
        "tc.read": f"tc_sha={tc}\ntc_branch=tc/pr-42\n".encode(),
        "shard.done": f"suite={suite}\nidx=00\nassigned=1\nctp_rc=0\n".encode(),
        "plan/expected": b"1",
        "plan/split.meta": f"tc_sha='{tc}'\n".encode(),
        "summary_info": (
            f"total:1\nsuccess:{int(outcome == 'PASS')}\n"
            f"fail:{int(outcome == 'FAIL')}\n"
            f"{suite}/:1 {int(outcome == 'PASS')} {int(outcome == 'FAIL')}\n"
            f"/home/cubrid-testcases/{case}:{verdict}    12ms\n"
        ).encode(),
    }
    metadata = {
        "repository": "CUBRID/cubrid",
        "completed_at": at,
        "execution_attempt": 1,
        "execution_job_id": run_id * 10,
        "engine_branch": "topic",
        "workflow_sha": "c" * 40,
    }
    if complete_context:
        metadata.update(
            ctp_sha="d" * 40,
            platform="linux/x86_64",
            configuration_digest="config-v1",
            execution_variant="default",
        )
    return Bundle(
        run_id,
        suite,
        "00",
        files,
        metadata,
        {"summary_info": f"http://example.test/runs/{run_id}/summary_info"},
    )


def run_metadata(run_id, *, attempt=1, created="2026-10-15T10:00:00Z"):
    return {
        "id": run_id,
        "repository": "CUBRID/cubrid",
        "run_attempt": attempt,
        "created_at": created,
        "status": "completed",
        "conclusion": "failure",
        "path": ".github/workflows/gha-ci.yml",
        "html_url": f"https://example.test/{run_id}",
        "head_sha": "c" * 40,
        "head_branch": "develop",
        "event": "issue_comment",
    }


class ParsingTests(unittest.TestCase):
    def test_recorded_public_summaries(self):
        for name, counts in [
            ("sql-recorded", {"PASS": 407, "FAIL": 1}),
            ("medium-recorded", {"PASS": 975}),
            ("sql-success-recorded", {"PASS": 349}),
        ]:
            with self.subTest(name=name):
                result = parse(read_bundle(FIXTURES / name / "bundle.json"))
                self.assertEqual(Counter(o.outcome for o in result.observations), counts)
                self.assertTrue(result.complete, result.diagnostics)
                self.assertEqual(result.engine_sha, "b74043dfabe0ad36ee4939353a8848a939f90184")

    def test_recorded_medium_failure_preserves_positive_evidence(self):
        result = parse(read_bundle(FIXTURES / "medium-failure-recorded/bundle.json"))
        self.assertEqual(sum(o.outcome == "FAIL" for o in result.observations), 3)
        self.assertTrue(result.complete, result.diagnostics)
        self.assertEqual(result.engine_sha, "b755678cd9a3ab465215228d5664f7f1edf24846")

    def test_sanitized_shell_formats(self):
        for name, counts in [
            ("shell-success-sanitized", {"PASS": 1, "SKIP": 1}),
            ("shell-failure-sanitized", {"PASS": 1, "FAIL": 1, "SKIP": 1}),
        ]:
            result = parse(read_bundle(FIXTURES / name / "bundle.json"))
            self.assertEqual(Counter(o.outcome for o in result.observations), counts)
            self.assertTrue(result.complete, result.diagnostics)

    def test_fixture_checksums(self):
        manifest = json.loads((FIXTURES / "provenance.json").read_text())
        for record in manifest["records"]:
            for filename, digest in record["sha256"].items():
                self.assertEqual(
                    hashlib.sha256(
                        (FIXTURES / record["directory"] / filename).read_bytes()
                    ).hexdigest(),
                    digest,
                )

    def test_path_normalization_preserves_suite_and_case(self):
        self.assertEqual(canonical_path("/home/cubrid-testcases/" + CASE, "sql"), CASE)
        for path in (
            "../" + CASE,
            "sql/../" + CASE,
            "/unknown/" + CASE,
            CASE.replace("sql/", "medium/", 1),
            "sql/_fixture/",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                canonical_path(path, "sql")
        self.assertEqual(
            canonical_path(CASE.replace("shared", "Shared"), "sql"),
            CASE.replace("shared", "Shared"),
        )

    def test_empty_failure_list_never_becomes_pass(self):
        bundle = sample()
        del bundle.files["summary_info"]
        bundle.files["failed.list"] = b""
        result = parse(bundle)
        self.assertEqual(result.observations, [])
        self.assertFalse(result.complete)

    def test_failure_list_fallback_is_partial(self):
        bundle = sample()
        del bundle.files["summary_info"]
        bundle.files["failed.list"] = ("00\t" + CASE + "\n").encode()
        result = parse(bundle)
        self.assertEqual([o.outcome for o in result.observations], ["FAIL"])
        self.assertFalse(result.complete)

    def test_missing_completion_and_count_conflicts_are_partial(self):
        bundle = sample()
        del bundle.files["shard.done"]
        bundle.files["plan/expected"] = b"20"
        result = parse(bundle)
        self.assertFalse(result.complete)
        self.assertIn("incomplete_shard", {d.reason for d in result.diagnostics})
        self.assertNotIn("NOT_RUN", [o.outcome for o in result.observations])

    def test_plan_revision_conflict_does_not_override_consumed_sha(self):
        bundle = sample()
        bundle.files["plan/split.meta"] = ("tc_sha='" + "e" * 40 + "'\n").encode()
        result = parse(bundle)
        self.assertEqual(result.testcase_sha, TC)
        self.assertIn("provenance_conflict", {d.reason for d in result.diagnostics})

    def test_repeated_records_remain_explicit_and_partial(self):
        bundle = sample()
        bundle.files["summary_info"] += bundle.files["summary_info"].splitlines()[-1] + b"\n"
        result = parse(bundle)
        self.assertEqual([o.ordinal for o in result.observations], [1, 2])
        self.assertIn("invocation_unresolved", {d.reason for d in result.diagnostics})

    def test_conflicting_failure_list_retains_both_outcomes(self):
        bundle = sample(outcome="PASS")
        bundle.files["failed.list"] = ("00\t" + CASE + "\n").encode()
        result = parse(bundle)
        self.assertEqual([o.outcome for o in result.observations], ["PASS", "FAIL"])
        self.assertFalse(result.complete)

    def test_shell_selection_without_a_verdict_keeps_execution_unknown(self):
        bundle = read_bundle(FIXTURES / "shell-success-sanitized/bundle.json")
        paths = [o.path for o in parse(bundle).observations]
        extra = "shell/_fixture/missing/cases/missing.sh"
        bundle.files["plan/shard.list"] = ("\n".join(paths + [extra]) + "\n").encode()
        result = parse(bundle)
        self.assertNotIn(extra, [o.path for o in result.observations])
        self.assertFalse(result.complete)

    def test_lost_shell_xml_does_not_label_executed_cases_not_run(self):
        bundle = read_bundle(FIXTURES / "shell-success-sanitized/bundle.json")
        paths = [o.path for o in parse(bundle).observations]
        bundle.files["plan/shard.list"] = ("\n".join(paths) + "\n").encode()
        del bundle.files["test-results/test-shell.xml"]
        result = parse(bundle)
        self.assertEqual(result.observations, [])
        self.assertIn("incomplete_shard", {d.reason for d in result.diagnostics})

    def test_missing_shard_counters_are_missing_evidence_not_unsupported_grammar(self):
        bundle = sample()
        del bundle.files["shard.done"]
        result = parse(bundle)
        self.assertIn("counts_unavailable", {d.reason for d in result.diagnostics})
        self.assertNotIn("parse_unsupported", {d.reason for d in result.diagnostics})

    def test_malformed_junit_does_not_infer_pass(self):
        bundle = read_bundle(FIXTURES / "shell-success-sanitized/bundle.json")
        for data in (b"<testsuite><testcase>", b"<!DOCTYPE x><testsuite/>"):
            bundle.files["test-results/test-shell.xml"] = data
            self.assertEqual(parse(bundle).observations, [])
            self.assertFalse(parse(bundle).complete)

    def test_source_mutation_quarantines_engine_attribution(self):
        bundle = sample()
        bundle.metadata["fetch_consistency"] = "changed_during_fetch"
        result = parse(bundle)
        self.assertEqual([o.outcome for o in result.observations], ["FAIL"])
        self.assertIsNone(result.engine_sha)
        self.assertFalse(result.complete)

    def test_junit_error_is_distinct_and_bad_duration_preserves_verdict(self):
        bundle = read_bundle(FIXTURES / "shell-failure-sanitized/bundle.json")
        xml = bundle.files["test-results/test-shell.xml"]
        bundle.files["test-results/test-shell.xml"] = (
            xml.replace(b"<failure", b"<error")
            .replace(b"</failure>", b"</error>")
            .replace(b'time="', b'time="invalid', 1)
        )
        result = parse(bundle)
        self.assertIn("ERROR", [o.outcome for o in result.observations])
        self.assertNotIn("FAIL", [o.outcome for o in result.observations])
        self.assertFalse(result.complete)


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary.name)
        self.index = Index(self.path, writable=True)
        self.query = Queries(self.index)

    def tearDown(self):
        self.index.__exit__()
        self.temporary.cleanup()

    def ingest(self, *args, **kwargs):
        return self.index.ingest(parse(sample(*args, **kwargs)))

    def complete_window(self, run_ids=(1,)):
        runs = [run_metadata(i) for i in run_ids]
        for run in runs:
            self.index.record_run(run, "2026-10-17T00:00:00Z")
        self.index.record_discovery(
            "CUBRID/cubrid", "2026-10-15T00:00:00Z", "2026-10-15T23:59:59Z", runs, True
        )

    def test_repeated_import_and_reparse_are_idempotent(self):
        receipt = self.ingest()
        self.assertTrue(receipt["inserted"])
        self.assertFalse(self.ingest()["inserted"])
        self.assertEqual(self.index.reparse(), {"snapshots": 1, "inserted": 0})
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM observation").fetchone()[0], 1)
        self.assertEqual(self.index.bundle(receipt["execution_id"]).files, sample().files)

    def test_reparse_retires_legacy_selection_only_not_run_rows(self):
        bundle = read_bundle(FIXTURES / "shell-success-sanitized/bundle.json")
        parsed = parse(bundle)
        missing = "shell/_fixture/missing/cases/missing.sh"
        bundle.files["plan/shard.list"] = (
            "\n".join([o.path for o in parsed.observations] + [missing]) + "\n"
        ).encode()
        parsed.parser_version = 1
        parsed.observations.append(
            Observation(missing, missing, "NOT_RUN", "planned_only", "plan/shard.list", "inventory")
        )
        legacy = self.index.ingest(parsed)["execution_id"]
        self.assertEqual(self.index.reparse()["inserted"], 1)
        rows = self.index.db.execute(
            "SELECT o.outcome FROM observation o JOIN execution e "
            "ON e.id=o.execution_id WHERE e.active=1"
        ).fetchall()
        self.assertEqual({r[0] for r in rows}, {"PASS", "SKIP"})
        self.assertEqual(len(rows), 2)
        self.assertIn("plan/shard.list", self.index.bundle(legacy).files)

    def test_completed_unique_job_enriches_ordering_without_duplicating_a_snapshot(self):
        bundle = sample()
        bundle.metadata = {"run_attempts": 1, "ordering": "unknown"}
        receipt = self.index.ingest(parse(bundle))
        first_manifest = self.index.db.execute(
            "SELECT manifest_digest FROM execution WHERE id=?", (receipt["execution_id"],)
        ).fetchone()[0]
        bundle.metadata.update(completed_at="2026-10-15T12:00:00Z", ordering="unique_job")
        later = self.index.ingest(parse(bundle))
        self.assertFalse(later["inserted"])
        self.assertEqual(later["execution_id"], receipt["execution_id"])
        target = self.query.history(HistoryQuery(CASE))["target"]
        self.assertEqual(target["completed_at"], bundle.metadata["completed_at"])
        self.assertIsNone(target["attempt"])
        self.assertEqual(target["metadata"]["previous_manifest_digest"], first_manifest)
        self.assertEqual(
            json.loads(self.index.archive.read(first_manifest))["metadata"]["ordering"], "unknown"
        )

    def test_transaction_failure_publishes_nothing(self):
        parsed = parse(sample())
        parsed.observations.append(parsed.observations[0])  # Violates the observation primary key.
        with self.assertRaises(sqlite3.IntegrityError):
            self.index.ingest(parsed)
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM execution").fetchone()[0], 0)
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM testcase").fetchone()[0], 0)
        self.assertTrue(self.ingest()["inserted"])

    def test_reparse_failure_keeps_previous_parser_generation(self):
        self.ingest(1)
        self.ingest(2)
        original = self.index.ingest
        calls = 0

        def fail_second(parsed):
            nonlocal calls
            calls += 1
            parsed.parser_version += 1
            if calls == 2:
                raise ValueError("simulated interrupted reparse")
            return original(parsed)

        generation = self.query.coverage()["generation"]
        with patch.object(self.index, "ingest", side_effect=fail_second):
            with self.assertRaisesRegex(ValueError, "interrupted reparse"):
                self.index.reparse()
        self.assertEqual(self.query.coverage()["generation"], generation)
        self.assertEqual(
            self.index.db.execute(
                "SELECT DISTINCT parser_version FROM execution WHERE active=1"
            ).fetchall()[0][0],
            PARSER_VERSION,
        )
        self.assertEqual(self.index.db.execute("SELECT count(*) FROM execution").fetchone()[0], 2)

    def test_corrupt_archive_refuses_reparse(self):
        receipt = self.ingest()
        digest = json.loads(
            self.index.archive.read(
                self.index.db.execute(
                    "SELECT manifest_digest FROM execution WHERE id=?", (receipt["execution_id"],)
                ).fetchone()[0]
            )
        )["files"]["summary_info"]
        target = self.path / "objects" / digest[:2] / (digest + ".gz")
        target.write_bytes(gzip.compress(b"corrupted"))
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.index.reparse()

    def test_query_snapshot_stays_consistent_during_write(self):
        self.ingest()
        with Index(self.path) as reader:
            before = Queries(reader).history(HistoryQuery(CASE))
            self.ingest(2)
            after = Queries(reader).history(HistoryQuery(CASE))
        self.assertEqual(before["occurrences"], after["occurrences"])
        self.assertEqual(before["coverage"]["generation"], after["coverage"]["generation"])

    def test_failure_then_green_retry_preserves_failure(self):
        self.ingest(1, "FAIL", at="2026-10-15T10:00:00Z")
        self.ingest(1, "PASS", at="2026-10-15T11:00:00Z")
        self.ingest(2, "FAIL", at="2026-10-15T12:00:00Z", tc="e" * 40)
        report = self.query.history(HistoryQuery(CASE, run_id=2))
        self.assertEqual(report["history_result"], "SEEN_BEFORE")
        self.assertEqual(report["prior_verified_executions"], 2)
        self.assertEqual(len(report["prior_failures"]), 1)
        self.assertEqual(report["failed_commits"], [ENGINE])
        self.assertNotEqual(
            report["target"]["engine_sha"], report["target"]["metadata"]["workflow_sha"]
        )

    def test_first_observed_requires_complete_window(self):
        self.ingest()
        self.assertEqual(
            self.query.history(HistoryQuery(CASE))["history_result"], "UNKNOWN_HISTORY"
        )
        self.complete_window()
        report = self.query.history(HistoryQuery(CASE))
        self.assertTrue(report["coverage"]["complete"], report["coverage"])
        self.assertEqual(report["history_result"], "FIRST_OBSERVED_IN_WINDOW")
        self.index.availability(
            "CUBRID/cubrid", 1, "sql", "summary_info", "access_denied_or_missing"
        )
        self.assertEqual(
            self.query.history(HistoryQuery(CASE))["history_result"], "UNKNOWN_HISTORY"
        )

    def test_single_indexed_run_without_discovery_is_incomplete(self):
        self.ingest()
        self.index.record_run(run_metadata(1), "2026-10-17T00:00:00Z")
        report = self.query.history(HistoryQuery(CASE))
        self.assertFalse(report["coverage"]["discovery_complete"])
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")

    def test_equal_completion_times_cannot_establish_novelty(self):
        self.ingest(1)
        self.ingest(2)
        self.complete_window((1, 2))
        report = self.query.history(HistoryQuery(CASE, run_id=2))
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")
        self.assertIn("ordering_unknown", report["reasons"])

    def test_newer_discovered_attempt_invalidates_stale_metadata(self):
        self.ingest()
        self.complete_window()
        self.index.record_discovery(
            "CUBRID/cubrid",
            "2026-10-15T00:00:00Z",
            "2026-10-15T23:59:59Z",
            [run_metadata(1, attempt=2)],
            True,
        )
        report = self.query.history(HistoryQuery(CASE))
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")
        self.assertEqual(report["coverage"]["stale_runs"], [1])

    def test_unknown_creation_date_does_not_match_an_explicit_window(self):
        self.ingest()
        report = self.query.history(HistoryQuery(CASE, since="2026-10-15", until="2026-10-15"))
        self.assertEqual(report["occurrences"], [])
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")
        self.assertIn("run_creation_unknown", report["reasons"])

    def test_known_and_unknown_variants_remain_separate(self):
        self.ingest()
        self.ingest(2, complete_context=True)
        self.assertEqual(len(self.query.find("shared.sql")), 2)
        with self.assertRaises(AmbiguousIdentity):
            self.query.history(HistoryQuery(CASE))
        report = self.query.history(HistoryQuery(CASE, variant="unknown"))
        self.assertIsNone(report["identity"]["variant"])
        self.assertEqual(len(report["occurrences"]), 1)

    def test_another_writer_is_rejected_but_readers_work(self):
        self.ingest()
        with self.assertRaisesRegex(ValueError, "another Chronicle writer"):
            Index(self.path, writable=True)
        with Index(self.path) as reader:
            self.assertEqual(len(Queries(reader).history(HistoryQuery(CASE))["occurrences"]), 1)

    def test_missing_job_snapshot_blocks_coverage(self):
        self.ingest()
        self.complete_window()
        run = run_metadata(1)
        run["jobs"] = [{"name": "shard sql 01", "conclusion": "success"}]
        self.index.record_run(run, "2026-10-17T00:00:00Z")
        coverage = self.query.coverage(suite="sql")
        self.assertFalse(coverage["complete"])
        self.assertEqual(coverage["missing_shards"], [{"run_id": 1, "suite": "sql", "shard": "01"}])

    def test_adjacent_discovery_windows_cover_without_hiding_gaps(self):
        self.ingest()
        self.index.record_run(run_metadata(1), "2026-10-17T00:00:00Z")
        for lo, hi in [("00:00:00", "11:59:59"), ("12:00:00", "23:59:59")]:
            self.index.record_discovery(
                "CUBRID/cubrid", f"2026-10-15T{lo}Z", f"2026-10-15T{hi}Z", [run_metadata(1)], True
            )
        self.assertTrue(self.query.coverage()["discovery_complete"])
        self.assertFalse(self.query.coverage(since="2026-10-14")["discovery_complete"])

    def test_unknown_historical_branch_does_not_match_workflow_branch(self):
        bundle = sample()
        del bundle.metadata["engine_branch"]
        bundle.metadata["workflow_branch"] = "develop"
        self.index.ingest(parse(bundle))
        report = self.query.history(HistoryQuery(CASE, branch="develop"))
        self.assertEqual(report["occurrences"], [])
        self.assertIn("historical_branch_unknown", report["reasons"])

    def test_missing_discovered_run_blocks_novelty(self):
        self.ingest()
        self.complete_window()
        self.index.record_discovery(
            "CUBRID/cubrid",
            "2026-10-15T00:00:00Z",
            "2026-10-15T23:59:59Z",
            [run_metadata(1), run_metadata(2)],
            True,
        )
        report = self.query.history(HistoryQuery(CASE))
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")
        self.assertIn(2, report["coverage"]["missing_runs"])

    def test_unknown_attempts_and_order_stay_unknown(self):
        bundle = sample()
        bundle.metadata = {}
        self.index.ingest(parse(bundle))
        self.complete_window()
        report = self.query.history(HistoryQuery(CASE))
        self.assertEqual(report["history_result"], "UNKNOWN_HISTORY")
        self.assertIsNone(report["target"]["attempt"])
        self.assertIn("ordering_unknown", report["coverage"]["reasons"])

    def test_same_basename_in_different_suites_is_ambiguous(self):
        self.ingest()
        self.ingest(suite="medium")
        self.assertEqual(len(self.query.find("shared.sql")), 2)
        with self.assertRaises(AmbiguousIdentity):
            self.query.history(HistoryQuery("shared.sql"))
        self.assertEqual(self.query.history(HistoryQuery(CASE))["identity"]["suite"], "sql")

    def test_unknown_engine_sha_excluded_from_commit_counts(self):
        bundle = sample()
        del bundle.files["build.read"]
        self.index.ingest(parse(bundle))
        report = self.query.history(HistoryQuery(CASE))
        self.assertEqual(len(report["occurrences"]), 1)
        self.assertEqual(report["failed_commits"], [])

    def test_unknown_ancestry_never_becomes_a_match(self):
        self.ingest()
        report = self.query.history(HistoryQuery(CASE, ancestor_of="e" * 40))
        self.assertEqual(report["occurrences"], [])
        self.assertIn("ancestry_unknown", report["reasons"])

    def test_inconsistent_outcomes_require_known_context(self):
        self.ingest(1, "FAIL", complete_context=True)
        self.ingest(2, "PASS", complete_context=True)
        report = self.query.history(HistoryQuery(CASE, variant="default"))
        self.assertEqual(len(report["inconsistent_observed_outcomes"]), 1)
        self.ingest(3, "PASS")
        report = self.query.history(HistoryQuery(CASE, variant="default"))
        self.assertEqual(len(report["inconsistent_observed_outcomes"][0]), 2)

    def test_cli_exit_codes_json_and_offline_queries(self):
        self.ingest()
        self.ingest(suite="medium")
        with patch("chronicle.collection.GitHub.get", side_effect=AssertionError("network")):
            for command, expected in [
                (["history", CASE, "--json"], 0),
                (["history", "shared.sql", "--json"], 3),
                (["evidence", "--execution", "999", "--json"], 2),
            ]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = main(["--data-dir", str(self.path), *command])
                self.assertEqual(code, expected)
                self.assertEqual(json.loads(out.getvalue() or err.getvalue())["schema_version"], 1)


class ReplayGitHub:
    def __init__(self, run, jobs):
        self.run, self.jobs = run, jobs
        self.requests = []

    def get(self, endpoint):
        self.requests.append(endpoint)
        return copy.deepcopy(self.run)

    def pages(self, endpoint, key):
        self.requests.append(endpoint)
        attempt = int(endpoint.split("/attempts/")[1].split("/")[0])
        return [copy.deepcopy(j) for j in self.jobs if j["run_attempt"] == attempt]

    def discover(self, repository, since, until):
        return [copy.deepcopy(self.run)]


class ReplayStore:
    def __init__(self, bundle):
        self.bundle = bundle

    def url(self, path):
        return "http://example.test/" + path

    def fetch(self, path):
        relative = path.split("/", 3)[3]
        if relative == "plan/plan.tsv":
            return b"00\t1\t1\t0\n", None
        name = "failed.list" if relative == "collect/failed.list" else relative
        if name.startswith("shard/00/"):
            name = name.removeprefix("shard/00/")
        if name == "plan/shards/00.list":
            return b"cubrid-testcases/sql/_fixture\n", None
        if name == "failed.list":
            return (
                ("00\t" + CASE + "\n").encode()
                if b":nok" in self.bundle.files["summary_info"]
                else b""
            ), None
        return (
            (self.bundle.files[name], None)
            if name in self.bundle.files
            else (None, "access_denied_or_missing")
        )


class CollectorTests(unittest.TestCase):
    def test_partial_rerun_does_not_copy_snapshot_into_each_attempt(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            Index(Path(directory), writable=True) as index,
        ):
            run = run_metadata(1, attempt=2)
            jobs = [
                {
                    "name": "shard sql 00",
                    "run_attempt": i,
                    "id": i * 10,
                    "status": "completed",
                    "conclusion": "failure",
                    "completed_at": f"2026-10-15T1{i}:00:00Z",
                }
                for i in (1, 2)
            ]
            github = ReplayGitHub(run, jobs)
            collector = Collector(index, github, ReplayStore(sample()), suites=("sql",))
            result = collector.sync(run_ids=[1], revisit_limit=0)
            self.assertEqual(result["inserted_snapshots"], 1)
            self.assertTrue(any("/attempts/1/" in r for r in github.requests))
            self.assertTrue(any("/attempts/2/" in r for r in github.requests))
            observation = Queries(index).history(HistoryQuery(CASE))["target"]
            self.assertIsNone(observation["attempt"])
            self.assertIsNone(observation["completed_at"])
            self.assertEqual(collector.sync(run_ids=[1], revisit_limit=0)["inserted_snapshots"], 0)

    def test_discovery_import_revisit_and_changed_result(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            Index(Path(directory), writable=True) as index,
        ):
            run = run_metadata(1)
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
            store = ReplayStore(sample())
            collector = Collector(index, ReplayGitHub(run, jobs), store, suites=("sql",))
            first = collector.sync(since="2026-10-15", until="2026-10-15", revisit_limit=0)
            self.assertEqual(first["observations"], 1)
            self.assertTrue(Queries(index).coverage()["discovery_complete"])
            store.bundle = sample(outcome="PASS")
            with index.db:
                index.db.execute("UPDATE run SET revisit_at='2000-01-01T00:00:00Z'")
            second = collector.sync(revisit_limit=1)
            self.assertEqual(second["inserted_snapshots"], 1)
            report = Queries(index).history(HistoryQuery(CASE))
            self.assertEqual({o["outcome"] for o in report["occurrences"]}, {"FAIL", "PASS"})

    def test_failed_discovery_records_incomplete_coverage(self):
        github = ReplayGitHub(run_metadata(1), [])
        with (
            tempfile.TemporaryDirectory() as directory,
            Index(Path(directory), writable=True) as index,
        ):
            with patch.object(github, "discover", side_effect=CollectionError("budget")):
                with self.assertRaises(CollectionError):
                    Collector(index, github, ReplayStore(sample())).sync(
                        since="2026-10-15", until="2026-10-15", revisit_limit=0
                    )
            self.assertFalse(Queries(index).coverage()["discovery_complete"])

    def test_paginated_and_split_discovery_deduplicates(self):
        github = GitHub()
        lo, hi = "2026-10-15T00:00:00Z", "2026-10-15T00:00:03Z"
        with patch.object(
            github,
            "get",
            side_effect=[
                {"total_count": 1000, "workflow_runs": []},
                {"total_count": 101, "workflow_runs": [{"id": i} for i in range(100)]},
                {"total_count": 101, "workflow_runs": [{"id": 100}]},
                {"total_count": 2, "workflow_runs": [{"id": 100}, {"id": 101}]},
            ],
        ) as get:
            runs = github.discover("CUBRID/cubrid", lo, hi)
        self.assertEqual(len(runs), 102)
        self.assertEqual(get.call_count, 4)

    def test_github_budget_and_utc_dates(self):
        with self.assertRaises(CollectionError):
            GitHub(0).get("repos/example/repo")
        self.assertEqual(timestamp("2026-10-15", end=True), "2026-10-15T23:59:59Z")
        self.assertEqual(timestamp("2026-10-15T09:00:00+09:00"), "2026-10-15T00:00:00Z")

    def test_rate_limit_stops_requests_and_reports_retry_after(self):
        response = type(
            "Response",
            (),
            {
                "returncode": 1,
                "stderr": b"gh: HTTP 429",
                "stdout": b"HTTP/2 429\r\nRetry-After: 120\r\n\r\n{}",
            },
        )()
        with patch("chronicle.collection.subprocess.run", return_value=response):
            with self.assertRaisesRegex(RequestLimitError, "Retry-After 120"):
                GitHub().get("repos/CUBRID/cubrid")

    def test_duplicate_page_ids_cannot_establish_complete_discovery(self):
        with patch.object(
            GitHub, "get", return_value={"total_count": 2, "workflow_runs": [{"id": 1}, {"id": 1}]}
        ):
            with self.assertRaises(CollectionError):
                GitHub().discover("CUBRID/cubrid", "2026-10-15T00:00:00Z", "2026-10-15T01:00:00Z")


if __name__ == "__main__":
    unittest.main()
