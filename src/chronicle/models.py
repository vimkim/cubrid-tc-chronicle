"""Values exchanged by the parser, index, collector, and query modules."""

from dataclasses import dataclass, field
from typing import Literal

PARSER_VERSION = 2
SUITES = ("sql", "medium", "shell")
# CI job names accepted wherever a suite is selected.
SUITE_ALIASES = {"test_sql": "sql", "test_medium": "medium", "test_shell": "shell"}
# Known CI suites that Chronicle does not collect.
UNSUPPORTED_SUITES = ("sql_by_cci", "test_sql_by_cci")
Outcome = Literal["PASS", "FAIL", "SKIP", "ERROR", "NOT_RUN"]


def testcase_repository(suite: str) -> str:
    return "CUBRID/cubrid-testcases-private-ex" if suite == "shell" else "CUBRID/cubrid-testcases"


@dataclass(frozen=True)
class Observation:
    path: str
    original: str
    outcome: Outcome
    raw_verdict: str
    evidence: str
    locator: str
    duration_ms: float | None = None
    ordinal: int = 1


@dataclass(frozen=True)
class Diagnostic:
    reason: str
    detail: str


@dataclass
class Bundle:
    """One source snapshot. Files use shard-relative names, never absolute paths.

    Plan files use the names plan/split.meta, plan/shard.list, and plan/expected.
    Missing files are absent. Metadata is evidence supplied by the collector.
    """

    run_id: int
    suite: str
    shard: str
    files: dict[str, bytes]
    metadata: dict = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedExecution:
    bundle: Bundle
    observations: list[Observation]
    diagnostics: list[Diagnostic]
    engine_sha: str | None
    testcase_sha: str | None
    testcase_branch: str | None
    build_mode: str | None
    complete: bool
    parser_version: int = PARSER_VERSION
