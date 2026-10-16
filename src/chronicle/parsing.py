"""Parse one retained shard snapshot, with conservative coverage and provenance.

Only case-level evidence creates outcomes. Counters constrain completeness.
Unknown formats remain diagnostics, and shell metadata is never executed.
"""

import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import PurePosixPath

from .models import SUITES, Bundle, Diagnostic, Observation, ParsedExecution, testcase_repository

_LEAF = re.compile(r"^(.+):([^\s:]+)\s+([0-9]+)ms$")
_SHA = re.compile(r"[0-9a-fA-F]{40}\Z")


def canonical_path(identifier: str, suite: str) -> str:
    """Normalize only observed runner roots and explicit repository prefixes."""
    if suite not in SUITES:
        raise ValueError(f"unsupported suite: {suite}")
    if "\\" in identifier or any(ord(c) < 32 for c in identifier):
        raise ValueError("invalid path character")
    if ".." in identifier.split("/"):
        raise ValueError("path traversal")
    slug = testcase_repository(suite).split("/", 1)[1]
    path = identifier
    for prefix in (f"/home/{slug}/", f"{slug}/", f"CUBRID/{slug}/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    if path.startswith("/"):
        raise ValueError("unrecognized absolute runner root")
    path = str(PurePosixPath(path))
    suffix = ".sh" if suite == "shell" else ".sql"
    if not path.startswith(suite + "/") or not path.endswith(suffix) or "/cases/" not in path:
        raise ValueError("identifier is not a testcase path in this suite")
    return path


def properties(data: bytes) -> dict[str, str]:
    """Read literal key=value records, including the plan's single quotes."""
    result = {}
    for line in data.decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", key):
            raise ValueError("malformed metadata record")
        if key in result:
            raise ValueError(f"duplicate metadata key: {key}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        result[key] = value
    return result


def parse(bundle: Bundle) -> ParsedExecution:
    if bundle.suite not in SUITES:
        raise ValueError(f"unsupported suite: {bundle.suite}")
    diagnostics: list[Diagnostic] = []
    observations: list[Observation] = []
    invocations: Counter[str] = Counter()

    def problem(reason, detail):
        diagnostic = Diagnostic(reason, detail)
        if diagnostic not in diagnostics:
            diagnostics.append(diagnostic)

    def props(name):
        try:
            return properties(bundle.files.get(name, b""))
        except (ValueError, UnicodeError) as e:
            problem("parse_unsupported", f"{name}: {e}")
            return {}

    def integer(value, source):
        if value is None:
            problem("counts_unavailable", f"{source}: count is missing")
            return None
        try:
            n = int(value)
            if n < 0:
                raise ValueError()
            return n
        except (ValueError, TypeError):
            problem("parse_unsupported", f"{source}: invalid nonnegative integer")
            return None

    def add(identifier, outcome, raw, evidence, locator, duration=None):
        try:
            path = canonical_path(identifier, bundle.suite)
        except ValueError as e:
            problem("identity_unresolved", f"{evidence}:{locator}: {e}")
            return
        invocations[path] += 1
        ordinal = invocations[path]
        if ordinal > 1:
            # Concatenated summary files do not prove that a repeated line is a retry.
            problem("invocation_unresolved", f"repeated testcase record: {path}")
        observations.append(
            Observation(path, identifier, outcome, raw, evidence, str(locator), duration, ordinal)
        )

    build, tc, plan, done = (
        props(f) for f in ("build.read", "tc.read", "plan/split.meta", "shard.done")
    )

    def sha(value, name):
        if not value or not _SHA.fullmatch(value):
            problem("provenance_unresolved", f"{name}: missing or invalid full SHA")
            return None
        return value.lower()

    engine_sha = sha(build.get("sha"), "build.read.sha")
    testcase_sha = sha(tc.get("tc_sha"), "tc.read.tc_sha")
    if bundle.metadata.get("fetch_consistency") == "changed_during_fetch":
        problem("provenance_conflict", "source files changed during collection")
        engine_sha = testcase_sha = None
    if plan.get("tc_sha") and testcase_sha != plan["tc_sha"].lower():
        problem("provenance_conflict", "plan and consumed testcase revisions disagree")
    # build.read.run_attempt is deliberately not an execution attempt.
    if not done:
        problem("incomplete_shard", "shard.done is absent or invalid")
    elif done.get("suite") != bundle.suite or done.get("idx") != bundle.shard:
        problem("provenance_conflict", "shard.done identifies another suite or shard")
    if done.get("ctp_rc") != "0":
        problem("runner_error", "CTP did not report successful completion")

    if bundle.suite in ("sql", "medium"):
        evidence = "summary_info"
        if evidence not in bundle.files:
            problem("results_unavailable", "summary_info is absent")
        else:
            try:
                lines = bundle.files[evidence].decode("utf-8").splitlines()
            except UnicodeError:
                lines = []
                problem("parse_unsupported", "summary_info is not UTF-8")
            for number, line in enumerate(lines, 1):
                match = _LEAF.fullmatch(line)
                if match:
                    identifier, verdict, duration = match.groups()
                    if verdict not in ("ok", "nok"):
                        problem("parse_unsupported", f"summary_info:{number}: verdict {verdict}")
                        continue
                    add(
                        identifier,
                        "PASS" if verdict == "ok" else "FAIL",
                        verdict,
                        evidence,
                        number,
                        float(duration),
                    )
                elif "/cases/" in line:
                    problem("parse_unsupported", f"summary_info:{number}: malformed leaf verdict")
            # Every summary level has counters. Only the suite-root row is shard-wide.
            roots = [line for line in lines if line.startswith(bundle.suite + "/:")]
            for line in roots:
                match = re.fullmatch(
                    re.escape(bundle.suite) + r"/:([0-9]+)\s+([0-9]+)\s+([0-9]+)", line
                )
                if not match:
                    problem("parse_unsupported", "malformed suite-root counter")
                    continue
                total, passed, failed = map(int, match.groups())
                actual = Counter(o.outcome for o in observations)
                if (len(observations), actual["PASS"], actual["FAIL"]) != (total, passed, failed):
                    problem("count_mismatch", "summary root counters disagree with leaf verdicts")
            if not roots:
                problem("counts_unavailable", "summary suite-root counters are absent")
    else:
        evidence = "test-results/test-shell.xml"
        xml = bundle.files.get(evidence)
        if xml is None:
            problem("results_unavailable", "shell JUnit XML is absent")
        else:
            try:
                # These constructs are unnecessary for JUnit. Reject entity expansion.
                xml_text = xml.decode("utf-8")
                if "<!DOCTYPE" in xml_text.upper() or "<!ENTITY" in xml_text.upper():
                    raise ValueError("DTD and entity declarations are unsupported")
                root = ET.fromstring(xml)
                if root.tag not in ("testsuite", "testsuites"):
                    raise ValueError("unsupported JUnit root")
                for number, case in enumerate(root.iter("testcase"), 1):
                    identifier = case.get("name", "")
                    file_identifier = case.get("file")
                    if file_identifier:
                        try:
                            if canonical_path(file_identifier, bundle.suite) != canonical_path(
                                identifier, bundle.suite
                            ):
                                problem(
                                    "identity_unresolved",
                                    f"JUnit testcase {number}: name/file disagree",
                                )
                                continue
                        except ValueError:
                            problem(
                                "identity_unresolved", f"JUnit testcase {number}: invalid name/file"
                            )
                            continue
                    tags = [t for t in ("failure", "error", "skipped") if case.find(t) is not None]
                    if len(tags) > 1:
                        problem(
                            "verdict_conflict", f"JUnit testcase {number}: conflicting outcomes"
                        )
                        continue
                    raw = tags[0] if tags else "success"
                    outcome = {"failure": "FAIL", "error": "ERROR", "skipped": "SKIP"}.get(
                        raw, "PASS"
                    )
                    duration = None
                    if case.get("time") is not None:
                        try:
                            duration = float(case.get("time")) * 1000
                            if not math.isfinite(duration) or duration < 0:
                                raise ValueError()
                        except ValueError:
                            problem(
                                "parse_unsupported", f"JUnit testcase {number}: invalid duration"
                            )
                            duration = None
                    add(identifier, outcome, raw, evidence, f"testcase[{number}]", duration)
                for suite in root.iter("testsuite"):
                    cases = list(suite.iter("testcase"))
                    executed = sum(c.find("skipped") is None for c in cases)
                    skipped = len(cases) - executed
                    for key, actual in (("tests", executed), ("skipped", skipped)):
                        if suite.get(key) is None:
                            problem("counts_unavailable", f"shell JUnit: missing {key} counter")
                        elif integer(suite.get(key), f"JUnit {key}") != actual:
                            problem("count_mismatch", f"shell JUnit {key} disagrees with cases")
            except (ET.ParseError, ValueError, UnicodeError) as e:
                problem("parse_unsupported", f"shell JUnit: {e}")

        status = props("test_status.data")
        counts = Counter(o.outcome for o in observations)
        expected_counts = {
            "total_case_count": len(observations),
            "total_executed_case_count": len(observations) - counts["SKIP"],
            "total_success_case_count": counts["PASS"],
            "total_fail_case_count": counts["FAIL"] + counts["ERROR"],
            "total_skip_case_count": counts["SKIP"],
        }
        for key, count in expected_counts.items():
            if key not in status:
                problem("counts_unavailable", f"test_status.data: missing {key}")
            elif integer(status[key], key) != count:
                problem("count_mismatch", f"{key}: counter disagrees with JUnit outcomes")

    if "failed.list" in bundle.files:
        try:
            failed = set()
            for number, line in enumerate(bundle.files["failed.list"].decode().splitlines(), 1):
                shard, sep, identifier = line.partition("\t")
                if not sep:
                    problem("parse_unsupported", f"failed.list:{number}: malformed row")
                    continue
                if shard != bundle.shard:
                    continue
                try:
                    path = canonical_path(identifier, bundle.suite)
                except ValueError as e:
                    problem("identity_unresolved", f"failed.list:{number}: {e}")
                    continue
                failed.add(path)
                matches = [o for o in observations if o.path == path]
                if not matches:
                    add(identifier, "FAIL", "failed.list", "failed.list", number)
                    problem(
                        "failure_list_only",
                        "failure evidence has no individual execution inventory",
                    )
                elif not any(o.outcome in ("FAIL", "ERROR") for o in matches):
                    problem(
                        "verdict_conflict", f"failed.list contradicts individual result: {path}"
                    )
                    add(identifier, "FAIL", "failed.list", "failed.list", number)
            explicit = {o.path for o in observations if o.outcome in ("FAIL", "ERROR")}
            if failed != explicit:
                problem("count_mismatch", "failed.list disagrees with individual failures")
        except UnicodeError:
            problem("parse_unsupported", "failed.list is not UTF-8")

    expected = bundle.files.get("plan/expected")
    assigned = integer(done.get("assigned"), "shard.done.assigned")
    if expected is not None:
        planned = integer(expected.decode(errors="replace").strip(), "plan count")
        if planned != assigned:
            problem("count_mismatch", "plan count disagrees with shard.done.assigned")
    else:
        problem("plan_unavailable", "expected shard count is absent")
    if assigned != len(observations):
        problem("count_mismatch", "assigned count disagrees with individual outcomes")
    if not observations:
        problem("no_observations", "no case-level verdicts were parsed")
    # Selection alone does not prove nonexecution. A missing verdict can also
    # mean lost, unavailable, or unpublished results from an executed testcase.
    if bundle.suite == "shell" and "plan/shard.list" in bundle.files:
        try:
            inventory = {
                canonical_path(line, bundle.suite)
                for line in bundle.files["plan/shard.list"].decode().splitlines()
                if line
            }
            observed = {o.path for o in observations}
            missing = len(inventory - observed)
            if missing:
                problem(
                    "incomplete_shard",
                    f"{missing} planned shell testcases have no retained "
                    "verdict; execution is unknown",
                )
            if assigned is not None and len(inventory) != assigned:
                problem("count_mismatch", "shell inventory disagrees with assigned count")
            if observed - inventory:
                problem("count_mismatch", "shell JUnit contains unplanned testcases")
        except (UnicodeError, ValueError) as e:
            problem("identity_unresolved", f"shell plan: {e}")

    return ParsedExecution(
        bundle,
        observations,
        diagnostics,
        engine_sha,
        testcase_sha,
        tc.get("tc_branch"),
        build.get("mode") or None,
        not diagnostics,
    )
