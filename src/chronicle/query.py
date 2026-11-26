"""Offline testcase resolution, history policy, and explicit coverage reports."""

import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .collection import timestamp
from .parsing import canonical_path
from .provenance import run_pull_request
from .status import assess, comparison_context
from .storage import Index, summarize_discovered, summarize_run

COVERAGE_GAP_LISTS = (
    "missing_runs",
    "stale_runs",
    "unverified_runs",
    "missing_shards",
    "gaps",
    "in_progress_runs",
)
COVERAGE_FORMS = ("compact", "full")


# Reasons a sync can clear. Every other reason (unresolved attempts, denied or
# missing files, parser diagnostics, ...) persists after a sync and is structural.
REFRESHABLE_REASONS = frozenset(
    {
        "discovery_incomplete",
        "discovery_stale",
        "discovery_freshness_unknown",
        "not_indexed",
        "metadata_stale",
        "run_metadata_unknown",
        "runs_in_progress",
        "source_unreachable",
    }
)
SYNC_RUN_LIMIT = 5


def split_reasons(reasons):
    """Return (refreshable, structural) sorted reason lists."""
    reasons = set(reasons)
    return (
        sorted(reasons & REFRESHABLE_REASONS),
        sorted(reasons - REFRESHABLE_REASONS),
    )


def sync_argv(runs, window, suite, missing=False, revisit=0):
    """Build the cubrid-tcc sync-data argv: a few run IDs, else an exact window.

    With missing, the window drains discovered-but-unimported runs without discovery.
    """
    argv = ["cubrid-tcc", "sync-data"]
    if missing and window and not runs:
        return (
            argv
            + ["--missing", "--since", window[0], "--until", window[1]]
            + (["--suite", suite] if suite else [])
        )
    if runs:
        argv += [item for run_id in runs for item in ("--run", str(run_id))]
    elif window:
        argv += ["--since", window[0], "--until", window[1]]
    else:
        return None
    if suite:
        argv += ["--suite", suite]
    return argv + ["--revisit-limit", "0"]


def shape_coverage(coverage, form="compact"):
    """Replace the suite-wide gap lists by their counts, unless the full form is requested."""
    if form not in COVERAGE_FORMS:
        raise ValueError("coverage form must be compact or full")
    if form == "full":
        return coverage
    compact = {k: v for k, v in coverage.items() if k not in COVERAGE_GAP_LISTS}
    compact["gap_counts"] = {k: len(coverage[k]) for k in COVERAGE_GAP_LISTS}
    return compact


class AmbiguousIdentity(ValueError):
    def __init__(self, candidates):
        super().__init__("testcase identity is ambiguous; select a full path and suite")
        self.candidates = candidates


@dataclass
class HistoryQuery:
    case: str
    suite: str | None = None
    run_id: int | None = None
    execution_id: int | None = None
    ordinal: int | None = None
    since: str | None = None
    until: str | None = None
    engine_sha: str | None = None
    branch: str | None = None
    outcome: str | None = None
    variant: str | None = None
    pr: int | None = None
    ancestor_of: str | None = None
    engine_repository: str | None = None
    coverage: str = "compact"


@dataclass
class StatusQuery:
    case: str
    suite: str | None = None
    variant: str | None = None
    branch: str | None = None
    engine_sha: str | None = None
    pr: int | None = None
    since: str | None = None
    until: str | None = None
    days: int | None = None
    max_age_hours: float = 24
    limit: int = 5


class Queries:
    def __init__(self, index: Index, repository="CUBRID/cubrid"):
        self.index = index
        self.db = index.db
        self.repository = repository
        self._run_cache = None

    def _runs(self):
        """Run summaries in run_id order. Parse metadata only for rows the columns lack.

        Rows are missing or stale when the index predates the query columns, or a
        Chronicle build without them replaced a run. Those fall back to the JSON.
        """
        generation = self.db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
        if self._run_cache is not None and self._run_cache[0] == generation:
            return self._run_cache[1]
        summaries, shards = {}, {}
        if self.index.layout:
            for r in self.db.execute(
                "SELECT run_id,metadata_digest,created_at,updated_at,status,run_attempt,"
                "has_jobs,pull_request FROM run_summary WHERE repository=?",
                (self.repository,),
            ):
                summaries[r["run_id"]] = r
            for r in self.db.execute(
                "SELECT run_id,suite,shard,conclusion FROM run_shard WHERE repository=? "
                "ORDER BY rowid",
                (self.repository,),
            ):
                shards.setdefault(r["run_id"], []).append((r["suite"], r["shard"], r["conclusion"]))
        runs = {}
        for r in self.db.execute(
            "SELECT run_id,metadata_digest FROM run WHERE repository=? ORDER BY run_id",
            (self.repository,),
        ).fetchall():
            row = summaries.get(r["run_id"])
            if row is not None and row["metadata_digest"] == r["metadata_digest"]:
                runs[r["run_id"]] = {
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                    "status": row["status"],
                    "run_attempt": row["run_attempt"],
                    "has_jobs": bool(row["has_jobs"]),
                    "pull_request": json.loads(row["pull_request"])
                    if row["pull_request"]
                    else None,
                    "shards": shards.get(r["run_id"], []),
                }
                continue
            metadata = json.loads(
                self.db.execute(
                    "SELECT metadata FROM run WHERE repository=? AND run_id=?",
                    (self.repository, r["run_id"]),
                ).fetchone()[0]
            )
            summary, parsed_shards = summarize_run(metadata)
            runs[r["run_id"]] = {
                **summary,
                "has_jobs": bool(summary["has_jobs"]),
                "pull_request": run_pull_request(metadata),
                "shards": parsed_shards,
            }
        self._run_cache = (generation, runs)
        return runs

    def _selected_runs(self, discovery, since, until):
        """Runs listed by complete discoveries inside the window; later discoveries win."""
        indexed = set()
        rows = {}
        if self.index.layout:
            indexed = {r[0] for r in self.db.execute("SELECT discovery_id FROM discovery_indexed")}
        by_discovery = {}
        if indexed:
            for r in self.db.execute(
                "SELECT discovery_id,run_id,created_at,run_attempt,updated_at,status "
                "FROM discovery_run"
            ):
                by_discovery.setdefault(r["discovery_id"], []).append(tuple(r)[1:])
        for record in discovery:
            if not record["complete"]:
                continue
            if record["id"] in indexed:
                found = by_discovery.get(record["id"], [])
            else:
                found = [
                    summarize_discovered(run)
                    for run in json.loads(self.index.archive.read(record["evidence_digest"]))
                ]
            for run_id, created_at, attempt, updated_at, status in found:
                if (since is None or created_at >= since) and (
                    until is None or created_at <= until
                ):
                    rows[run_id] = {
                        "created_at": created_at,
                        "run_attempt": attempt,
                        "updated_at": updated_at,
                        "status": status,
                    }
        return rows

    def overview(self, suite=None):
        """Summarize the same retained observations available to history queries."""
        counts = self.db.execute(
            """SELECT count(*) observations,count(DISTINCT o.testcase_id) testcases,
            count(DISTINCT CASE WHEN o.outcome IN ('FAIL','ERROR') THEN o.testcase_id END)
                failed_testcases,
            count(DISTINCT CASE WHEN o.outcome IN ('FAIL','ERROR') THEN e.engine_sha END)
                failed_commits
            FROM observation o JOIN execution e ON e.id=o.execution_id
            WHERE e.active=1 AND e.repository=? AND (? IS NULL OR e.suite=?)""",
            (self.repository, suite, suite),
        ).fetchone()
        coverage = self.coverage(suite=suite)
        return {
            "schema_version": 1,
            "counts": {
                **dict(counts),
                "runs": self.db.execute(
                    "SELECT count(*) FROM run WHERE repository=?", (self.repository,)
                ).fetchone()[0],
                "snapshots": coverage["executions"],
            },
            "coverage": coverage,
        }

    def catalog(self, term="", suite=None, *, failed_only=True, limit=40, offset=0):
        """Page testcase identities with observed failure counts, without merging variants."""
        if not 1 <= limit <= 100 or offset < 0:
            raise ValueError("catalog limit must be 1–100; offset must be nonnegative")
        parameters = (self.repository, term, suite, suite)
        where = """e.active=1 AND e.repository=? AND instr(lower(t.path),lower(?))>0
                   AND (? IS NULL OR t.suite=?)"""
        source = """testcase t JOIN observation o ON o.testcase_id=t.id
                    JOIN execution e ON e.id=o.execution_id"""
        if failed_only:
            where += " AND o.outcome IN ('FAIL','ERROR')"
            # Keep rare verdicts first. Otherwise SQLite can scan every outcome
            # in each active execution before applying the failure filter.
            source = """observation o CROSS JOIN execution e ON e.id=o.execution_id
                        CROSS JOIN testcase t ON t.id=o.testcase_id"""
        elif term:
            # A path search checks identities once before reading their outcomes.
            source = """testcase t CROSS JOIN observation o ON o.testcase_id=t.id
                        CROSS JOIN execution e ON e.id=o.execution_id"""
        total = self.db.execute(
            f"SELECT count(DISTINCT t.id) FROM {source} WHERE {where}",
            parameters,
        ).fetchone()[0]
        rows = self.db.execute(
            f"""SELECT t.*,count(*) observations,
            sum(o.outcome IN ('FAIL','ERROR')) failures,
            count(DISTINCT CASE WHEN o.outcome IN ('FAIL','ERROR') THEN e.engine_sha END)
                failed_commits,max(e.completed_at) last_completed_at
            FROM {source} WHERE {where}
            GROUP BY t.id ORDER BY max(e.completed_at) DESC,t.path,t.variant LIMIT ? OFFSET ?""",
            (*parameters, limit, offset),
        )
        return {
            "schema_version": 1,
            "total": total,
            "limit": limit,
            "offset": offset,
            "cases": [{**dict(r), "variant": r["variant"] or None} for r in rows],
            "generation": int(
                self.db.execute("SELECT value FROM meta WHERE key='generation'").fetchone()[0]
            ),
        }

    def find(self, term, suite=None, variant=None):
        if variant == "unknown":
            variant = ""
        for name in [suite] if suite else ("sql", "medium", "shell"):
            try:
                term = canonical_path(term, name)
                break
            except ValueError:
                pass
        rows = self.db.execute(
            "SELECT * FROM testcase WHERE instr(lower(path),lower(?))>0 "
            "AND (? IS NULL OR suite=?) AND (? IS NULL OR variant=?) ORDER BY suite,path",
            (term, suite, suite, variant, variant),
        )
        return [{**dict(row), "variant": row["variant"] or None} for row in rows]

    def _resolve(self, term, suite, variant=None):
        if variant == "unknown":
            variant = ""
        paths = {term}
        for name in [suite] if suite else ("sql", "medium", "shell"):
            try:
                paths.add(canonical_path(term, name))
            except ValueError:
                pass
        exact = []
        for path in paths:
            exact.extend(
                dict(r)
                for r in self.db.execute(
                    "SELECT * FROM testcase WHERE path=? AND (? IS NULL OR suite=?) "
                    "AND (? IS NULL OR variant=?)",
                    (path, suite, suite, variant, variant),
                )
            )
        candidates = list({r["id"]: r for r in exact}.values()) or self.find(term, suite, variant)
        if len(candidates) > 1:
            raise AmbiguousIdentity(candidates)
        if not candidates:
            return None
        return {**candidates[0], "variant": candidates[0]["variant"] or None}

    def coverage(self, since=None, until=None, suite=None, tail_tolerance_hours=0):
        """Report gaps. A discovery tail younger than the tolerance is not refreshable."""
        since = timestamp(since) if since else None
        until = timestamp(until, end=True) if until else None
        if since and until and since > until:
            raise ValueError("--since must not be later than --until")
        meta = dict(self.db.execute("SELECT key,value FROM meta"))
        discovery = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM discovery WHERE repository=? ORDER BY checked_at,id",
                (self.repository,),
            )
        ]
        runs = self._runs()
        starts = [r["since"] for r in discovery] + [r["created_at"] for r in runs.values()]
        ends = [r["until"] for r in discovery] + [r["created_at"] for r in runs.values()]
        since = since or (min(starts) if starts else None)
        until = until or (max(ends) if ends else None)
        reasons = set()
        intervals = sorted((r["since"], r["until"]) for r in discovery if r["complete"])
        cursor = (
            timestamp(
                (
                    datetime.fromisoformat(since.replace("Z", "+00:00")) - timedelta(seconds=1)
                ).isoformat()
            )
            if since
            else None
        )
        for lo, hi in intervals:
            adjacent = (
                timestamp(
                    (
                        datetime.fromisoformat(cursor.replace("Z", "+00:00")) + timedelta(seconds=1)
                    ).isoformat()
                )
                if cursor
                else None
            )
            if cursor is not None and lo <= adjacent and cursor <= hi:
                cursor = max(cursor, hi)
        discovery_complete = bool(since and until and cursor >= until)
        if not discovery_complete:
            reasons.add("discovery_incomplete")
        selected_runs = self._selected_runs(discovery, since, until)
        missing = sorted(set(selected_runs) - set(runs))
        stale = [
            run_id
            for run_id, discovered in selected_runs.items()
            if run_id in runs
            and (
                discovered["run_attempt"] > runs[run_id]["run_attempt"]
                or discovered["updated_at"] > runs[run_id]["updated_at"]
                or (
                    discovered["status"] != runs[run_id]["status"]
                    and discovered["updated_at"] >= runs[run_id]["updated_at"]
                )
            )
        ]
        if missing:
            reasons.add("not_indexed")
        if stale:
            reasons.add("metadata_stale")
        executions = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM execution WHERE active=1 AND repository=? "
                "AND (? IS NULL OR suite=?)",
                (self.repository, suite, suite),
            )
        ]

        def in_scope(run_id):
            run = runs.get(run_id) or selected_runs.get(run_id)
            # Imported snapshots without discovery metadata cannot prove a complete window.
            return run is None or (
                (since is None or run["created_at"] >= since)
                and (until is None or run["created_at"] <= until)
            )

        executions = [e for e in executions if in_scope(e["run_id"])]
        unverified_runs = sorted({e["run_id"] for e in executions if e["run_id"] not in runs})
        if unverified_runs:
            reasons.add("run_metadata_unknown")
        known_shards = {(e["run_id"], e["suite"], e["shard"]) for e in executions}
        missing_shards = []
        for run_id, run in runs.items():
            if not in_scope(run_id):
                continue
            if not run["has_jobs"]:
                # Explicit replay manifests can provide an empty inventory.
                continue
            for job_suite, shard, conclusion in run["shards"]:
                if suite and job_suite != suite:
                    continue
                key = (run_id, job_suite, shard)
                if conclusion != "skipped" and key not in known_shards:
                    missing_shards.append({"run_id": run_id, "suite": job_suite, "shard": shard})
        if missing_shards:
            reasons.add("not_indexed")
        gaps = [
            dict(r)
            for r in self.db.execute(
                "SELECT * FROM availability WHERE repository=? "
                "AND (? IS NULL OR suite IN (?, '*'))",
                (self.repository, suite, suite),
            )
            if in_scope(r["run_id"])
        ]
        reasons.update(g["reason"] for g in gaps)
        partial = [e for e in executions if not e["complete"]]
        for e in partial:
            reasons.update(d["reason"] for d in json.loads(e["diagnostics"]))
        unresolved_attempts = sum(e["attempt"] is None for e in executions)
        unresolved_revisions = sum(not e["engine_sha"] or not e["testcase_sha"] for e in executions)
        unordered = sum(e["completed_at"] is None for e in executions)
        if unresolved_attempts:
            reasons.add("attempt_unresolved")
        if unordered:
            reasons.add("ordering_unknown")
        if unresolved_revisions:
            reasons.add("provenance_unresolved")
        in_progress = [
            run_id for run_id, r in runs.items() if in_scope(run_id) and r["status"] != "completed"
        ]
        if in_progress:
            reasons.add("runs_in_progress")
        refreshable, structural = split_reasons(reasons)
        gap_start = None
        if not discovery_complete and since and until and cursor is not None:
            gap_start = timestamp(
                (
                    datetime.fromisoformat(cursor.replace("Z", "+00:00")) + timedelta(seconds=1)
                ).isoformat()
            )
            tail = datetime.fromisoformat(until) - datetime.fromisoformat(gap_start)
            if tail <= timedelta(hours=tail_tolerance_hours):
                # Fresh discovery lags a live window end by design; a sync would not close it.
                gap_start = None
                refreshable.remove("discovery_incomplete")
                structural.append("discovery_incomplete")
                structural.sort()
        refresh_runs = sorted(
            set(missing)
            | set(stale)
            | set(unverified_runs)
            | set(in_progress)
            | {m["run_id"] for m in missing_shards}
            | {g["run_id"] for g in gaps if g["reason"] in REFRESHABLE_REASONS}
        )
        spans = [
            (runs.get(r) or selected_runs.get(r) or {}).get("created_at") for r in refresh_runs
        ]
        spans = [t for t in spans if t]
        if gap_start:
            spans += [gap_start, until]
        window = (min(spans), max(spans)) if spans else None
        use_runs = refresh_runs if refresh_runs and len(refresh_runs) <= SYNC_RUN_LIMIT else []
        if gap_start:
            use_runs = []
        # Discovered-but-unimported runs drain from the queue instead of rediscovering the
        # window. In-progress and stale runs are due in the revisit queue, so ask for
        # revisits too; any other refreshable run shows up in the suggestion after the drain.
        others = set(refresh_runs) - set(missing)
        drain = bool(missing) and not gap_start and not use_runs
        revisit = bool(others & (set(in_progress) | set(stale)))
        if drain:
            made = [(selected_runs.get(r) or {}).get("created_at") for r in missing]
            window = (min(made), max(made))
        return {
            "source": "gha-ci",
            "repository": self.repository,
            "window": {"since": since, "until": until, "basis": "run_creation_utc"},
            "discovery_complete": discovery_complete,
            "enumerated_runs": len(selected_runs),
            "missing_runs": missing,
            "stale_runs": sorted(stale),
            "unverified_runs": unverified_runs,
            "missing_shards": missing_shards,
            "executions": len(executions),
            "complete_results": len(executions) - len(partial),
            "partial_results": len(partial),
            "unresolved_attempts": unresolved_attempts,
            "unresolved_revisions": unresolved_revisions,
            "unordered_executions": unordered,
            "in_progress_runs": in_progress,
            "gaps": gaps,
            "reasons": sorted(reasons),
            "refreshable_reasons": refreshable,
            "structural_reasons": structural,
            "needs_refresh": bool(refreshable),
            "suggested_sync": sync_argv(use_runs, window, suite, drain, 20 if revisit else 0)
            if refreshable
            else None,
            "complete": not reasons,
            "excluded_sources": ["CircleCI", "QA portal", "unit tests"],
            "upstream_limit": "Runs deleted before initial collection may be undetectable.",
            "last_sync": meta.get("last_sync"),
            "generation": int(meta["generation"]),
            "earliest_retained_execution": min(
                (e["completed_at"] for e in executions if e["completed_at"]), default=None
            ),
        }

    @staticmethod
    def _ancestor(sha, target, repository):
        if not sha or not target:
            return None
        if sha == target:
            return True
        if not repository:
            return None
        try:
            process = subprocess.run(
                ["git", "-C", repository, "merge-base", "--is-ancestor", sha, target],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return {0: True, 1: False}.get(process.returncode)

    def _occurrences(self, identity, query):
        """Read scoped observations for history and status through the same filters."""
        rows = self.db.execute(
            """SELECT e.*,o.ordinal,o.outcome,o.duration_ms,o.original,o.raw_verdict,
            o.evidence,o.locator
            FROM observation o JOIN execution e ON e.id=o.execution_id
            WHERE o.testcase_id=? AND e.active=1 AND e.repository=?
            ORDER BY e.completed_at,e.first_seen,e.id,o.ordinal""",
            (identity["id"], self.repository),
        )
        occurrences, unresolved_ancestry = [], 0
        unresolved_filters = set()
        runs = self._runs()
        run_dates = {run_id: run["created_at"] for run_id, run in runs.items()}
        for row in rows:
            record = dict(row)
            record["metadata"] = json.loads(record["metadata"])
            record["diagnostics"] = json.loads(record["diagnostics"])
            m = record["metadata"]
            record["pull_request"] = (runs.get(record["run_id"]) or {}).get("pull_request")
            if record["pull_request"] is None and m.get("engine_pr"):
                record["pull_request"] = {
                    "number": m["engine_pr"],
                    "url": f"https://github.com/{self.repository}/pull/{m['engine_pr']}",
                    "title": None,
                    "base_ref": None,
                    "head_ref": None,
                    "author": None,
                    "author_url": None,
                    "association": "execution_metadata",
                }
            record["engine_branch"] = m.get("engine_branch")
            if m.get("push_head_sha") == record["engine_sha"]:
                record["engine_branch"] = m.get("push_branch")
            created_at = run_dates.get(record["run_id"])
            if (query.since or query.until) and not created_at:
                unresolved_filters.add("run_creation_unknown")
                continue
            if created_at and (
                (query.since and created_at < timestamp(query.since))
                or (query.until and created_at > timestamp(query.until, end=True))
            ):
                continue
            if query.engine_sha and not (record["engine_sha"] or "").startswith(query.engine_sha):
                if not record["engine_sha"]:
                    unresolved_filters.add("provenance_unresolved")
                continue
            if query.branch and record["engine_branch"] != query.branch:
                if not record["engine_branch"]:
                    unresolved_filters.add("historical_branch_unknown")
                continue
            pr_number = (record["pull_request"] or {}).get("number")
            if query.pr is not None and pr_number != query.pr:
                if pr_number is None:
                    unresolved_filters.add("historical_pr_unknown")
                continue
            if query.outcome and record["outcome"] != query.outcome:
                continue
            if query.ancestor_of:
                ancestry = self._ancestor(
                    record["engine_sha"], query.ancestor_of, query.engine_repository
                )
                if ancestry is None:
                    unresolved_ancestry += 1
                if ancestry is not True:
                    continue
            manifest = json.loads(self.index.archive.read(record["manifest_digest"]))
            record["source"] = manifest["sources"].get(record["evidence"])
            record["evidence_digest"] = manifest["files"].get(record["evidence"])
            occurrences.append(record)
        if unresolved_ancestry:
            unresolved_filters.add("ancestry_unknown")
        return occurrences, sorted(unresolved_filters)

    def absence(self, identity, suite, coverage, windowed):
        """Say why a testcase has no observations in scope, and how fresh the suite's evidence is.

        suite_last_sync is the newest recorded execution completion. A revisit of an
        old run refreshes last_seen, so last_seen alone would hide a collection gap.
        """
        last_completed, last_checked = self.db.execute(
            "SELECT max(completed_at),max(last_seen) FROM execution "
            "WHERE active=1 AND repository=? AND (? IS NULL OR suite=?)",
            (self.repository, suite, suite),
        ).fetchone()
        if coverage["complete_results"] == 0 and coverage["partial_results"] > 0:
            # Failure lists record no passes, so absence only means "did not fail".
            reason = "suite_failures_only"
        elif identity is None:
            reason = "never_observed"
        elif windowed:
            reason = "observed_outside_window"
        else:
            reason = None
        return {
            "not_indexed_reason": reason,
            "suite_last_sync": last_completed,
            "suite_last_checked": last_checked,
        }

    def history(self, query: HistoryQuery):
        if query.coverage not in COVERAGE_FORMS:
            raise ValueError("coverage form must be compact or full")
        identity = self._resolve(query.case, query.suite, query.variant)
        coverage = self.coverage(
            query.since, query.until, query.suite or (identity or {}).get("suite")
        )
        report = {
            "schema_version": 2,
            "identity": identity,
            "target": None,
            "history_result": "UNKNOWN_HISTORY",
            "prior_verified_executions": 0,
            "prior_failures": [],
            "failed_commits": [],
            "occurrences": [],
            "coverage": shape_coverage(coverage, query.coverage),
            "comparison_groups": {},
            "inconsistent_observed_outcomes": [],
            "scope": {
                "branches": query.branch or "all indexed branches",
                "engine_sha": query.engine_sha,
                "ancestor_of": query.ancestor_of,
                "outcome": query.outcome,
                "pr": query.pr,
            },
            "reasons": [],
            "target_run_indexed": None,
            "suggested_sync": None,
            "not_indexed_reason": None,
            "suite_last_sync": None,
            "suite_last_checked": None,
        }
        if query.run_id is not None:
            run_suite = query.suite or (identity or {}).get("suite")
            indexed = query.run_id in self._runs() or bool(
                self.db.execute(
                    "SELECT 1 FROM execution WHERE repository=? AND run_id=? LIMIT 1",
                    (self.repository, query.run_id),
                ).fetchone()
            )
            report["target_run_indexed"] = indexed
            if not indexed:
                report["suggested_sync"] = sync_argv([query.run_id], None, run_suite)
        windowed = bool(query.since or query.until)
        if identity is None:
            report["reasons"] = ["not_indexed"]
            report.update(self.absence(None, query.suite, coverage, windowed))
            return report
        occurrences, limits = self._occurrences(identity, query)
        report["occurrences"] = occurrences
        if not occurrences:
            report.update(
                self.absence(identity, query.suite or identity["suite"], coverage, windowed)
            )
        report["reasons"].extend(limits)
        unresolved_ancestry = "ancestry_unknown" in limits
        unresolved_filters = limits
        run_dates = {run_id: run["created_at"] for run_id, run in self._runs().items()}
        failures = [o for o in occurrences if o["outcome"] in ("FAIL", "ERROR")]
        report["failed_commits"] = sorted({o["engine_sha"] for o in failures if o["engine_sha"]})
        targets = failures
        if query.run_id is not None:
            targets = [o for o in targets if o["run_id"] == query.run_id]
        if query.execution_id is not None:
            targets = [o for o in targets if o["id"] == query.execution_id]
        if query.ordinal is not None:
            targets = [o for o in targets if o["ordinal"] == query.ordinal]
        if not targets:
            report["reasons"].append("target_failure_not_indexed")
            return report
        if (query.run_id is not None or query.execution_id is not None) and len(targets) > 1:
            raise AmbiguousIdentity(
                [
                    {
                        "execution_id": o["id"],
                        "ordinal": o["ordinal"],
                        "run_id": o["run_id"],
                        "completed_at": o["completed_at"],
                    }
                    for o in targets
                ]
            )
        target = targets[-1]
        report["target"] = target
        report["target_selection"] = (
            "explicit" if query.run_id or query.execution_id else "latest_indexed_failure"
        )
        time = target["completed_at"]
        ambiguous_order = any(
            o["id"] != target["id"]
            and o["completed_at"] == time
            and o["outcome"] in ("FAIL", "ERROR")
            for o in occurrences
        )
        prior = [
            o
            for o in occurrences
            if time and o["completed_at"] and o["completed_at"] < time and o["id"] != target["id"]
        ]
        report["prior_verified_executions"] = sum(
            o["outcome"] in ("PASS", "FAIL", "ERROR") for o in prior
        )
        prior_failures = [o for o in prior if o["outcome"] in ("FAIL", "ERROR")]
        # Each prior failure is also in occurrences, so IDs are enough to look it up.
        report["prior_failures"] = [o["id"] for o in prior_failures]
        if prior_failures:
            report["history_result"] = "SEEN_BEFORE"
        elif (
            coverage["complete"]
            and time
            and target["engine_sha"]
            and not unresolved_ancestry
            and not unresolved_filters
            and not ambiguous_order
            and all(o["completed_at"] for o in occurrences)
            and run_dates.get(target["run_id"]) is not None
            and coverage["window"]["since"]
            <= run_dates[target["run_id"]]
            <= coverage["window"]["until"]
        ):
            report["history_result"] = "FIRST_OBSERVED_IN_WINDOW"
        else:
            report["reasons"].extend(coverage["reasons"])
            if not time:
                report["reasons"].append("target_ordering_unknown")
            if ambiguous_order:
                report["reasons"].append("ordering_unknown")
        groups = {"ancestors": [], "same_branch": [], "other_or_unknown_branch": []}
        target_context = comparison_context(target)
        for previous in prior_failures:
            context = comparison_context(previous)
            comparison = {
                key: (
                    "unknown"
                    if context[key] is None or target_context[key] is None
                    else "equal"
                    if context[key] == target_context[key]
                    else "different"
                )
                for key in context
            }
            ancestry = self._ancestor(
                previous["engine_sha"], target["engine_sha"], query.engine_repository
            )
            group = "other_or_unknown_branch"
            if ancestry is True:
                group = "ancestors"
            elif previous["engine_branch"] and previous["engine_branch"] == target["engine_branch"]:
                group = "same_branch"
            groups[group].append(
                {"execution_id": previous["id"], "ancestry": ancestry, "context": comparison}
            )
        report["comparison_groups"] = groups
        by_context = {}
        for o in occurrences:
            context = comparison_context(o)
            if not o["engine_sha"] or any(v is None for v in context.values()):
                continue
            key = (o["engine_sha"], *context.values())
            by_context.setdefault(key, []).append(o)
        for group in by_context.values():
            outcomes = {o["outcome"] for o in group}
            if "PASS" in outcomes and outcomes.intersection(("FAIL", "ERROR")):
                report["inconsistent_observed_outcomes"].append(sorted({o["id"] for o in group}))
        report["reasons"] = sorted(set(report["reasons"]))
        return report

    def status(self, query: StatusQuery, *, now=None):
        """Assess recent retained evidence, without refreshing sources or selecting a failure."""
        if query.days is not None and (query.days < 1 or query.since is not None):
            raise ValueError("--days must be positive and cannot be combined with --since")
        if not 1 <= query.limit <= 100 or not 0 < query.max_age_hours <= 8760:
            raise ValueError("--limit must be 1–100; --max-age-hours must be >0 and <=8760")
        evaluated_at = timestamp((now or datetime.now(UTC)).isoformat())
        until = timestamp(query.until, end=True) if query.until else evaluated_at
        since = (
            timestamp(query.since)
            if query.since
            else timestamp(
                (datetime.fromisoformat(until) - timedelta(days=query.days or 14)).isoformat()
            )
        )
        identity = self._resolve(query.case, query.suite, query.variant)
        suite = query.suite or (identity or {}).get("suite")
        coverage = self.coverage(since, until, suite, tail_tolerance_hours=query.max_age_hours)
        history_query = HistoryQuery(
            query.case,
            suite=suite,
            variant=query.variant,
            since=since,
            until=until,
            branch=query.branch,
            engine_sha=query.engine_sha,
            pr=query.pr,
        )
        occurrences, limits = self._occurrences(identity, history_query) if identity else ([], [])
        summary = assess(occurrences, query.limit)
        if identity is None:
            summary["status"] = "NOT_INDEXED"
            summary["reasons"] = ["not_indexed"]
        absence = {"not_indexed_reason": None, "suite_last_sync": None, "suite_last_checked": None}
        if identity is None or not occurrences:
            unfiltered = not (query.branch or query.engine_sha or query.pr or query.variant)
            absence = self.absence(identity, suite, coverage, unfiltered)
        if limits:
            # Unknown dates or engine references can hide a newer scoped outcome.
            summary["status"] = "UNKNOWN"
        discovery = self.db.execute(
            "SELECT until,checked_at FROM discovery WHERE repository=? AND complete=1 "
            "ORDER BY min(until,checked_at) DESC LIMIT 1",
            (self.repository,),
        ).fetchone()
        discovery_through = discovery["until"] if discovery else None
        discovery_checked_at = discovery["checked_at"] if discovery else None

        def age_hours(at):
            return (
                max(
                    0,
                    (
                        datetime.fromisoformat(evaluated_at) - datetime.fromisoformat(at)
                    ).total_seconds()
                    / 3600,
                )
                if at
                else None
            )

        def freshness_state(age):
            return "UNKNOWN" if age is None else "STALE" if age > query.max_age_hours else "FRESH"

        discovery_age = (
            max(age_hours(discovery_through), age_hours(discovery_checked_at))
            if discovery
            else None
        )
        observation_age = age_hours(summary["latest_observed_at"])
        discovery_state = freshness_state(discovery_age)
        observation_state = freshness_state(observation_age)
        states = (discovery_state, observation_state)
        freshness = "STALE" if "STALE" in states else "UNKNOWN" if "UNKNOWN" in states else "FRESH"
        reasons = set(summary["reasons"]) | set(limits) | set(coverage["reasons"])
        if discovery_state != "FRESH":
            reasons.add(
                "discovery_stale" if discovery_age is not None else "discovery_freshness_unknown"
            )
        if observation_state != "FRESH":
            reasons.add(
                "observation_stale"
                if observation_age is not None
                else "observation_freshness_unknown"
            )
        # Only coverage and discovery age are scoped to a sync. A testcase-level
        # reason such as an unknown identity stays structural.
        refreshable = sorted(
            set(coverage["refreshable_reasons"])
            | (reasons & {"discovery_stale", "discovery_freshness_unknown"})
        )
        structural = sorted(reasons - set(refreshable))
        needs_refresh = bool(refreshable)
        suggested = coverage["suggested_sync"]
        if needs_refresh and suggested is None and discovery_state != "FRESH":
            # Nothing is listed as missing, but discovery is old: scan from its end.
            start = discovery_through or since
            suggested = sync_argv([], (max(start, since), until), suite)
        return {
            "schema_version": 1,
            **summary,
            "requested_case": query.case,
            "identity": identity,
            "window": coverage["window"],
            "scope": {
                "repository": self.repository,
                "suite": suite,
                "branch": query.branch,
                "engine_sha": query.engine_sha,
                "pr": query.pr,
                "variant": query.variant,
            },
            "freshness": {
                "state": freshness,
                "evaluated_at": evaluated_at,
                "discovery_through": discovery_through,
                "discovery_checked_at": discovery_checked_at,
                "discovery_state": discovery_state,
                "discovery_age_hours": round(discovery_age, 3)
                if discovery_age is not None
                else None,
                "observation_state": observation_state,
                "observation_age_hours": round(observation_age, 3)
                if observation_age is not None
                else None,
                "max_age_hours": query.max_age_hours,
                "last_sync": coverage["last_sync"],
                "needs_refresh": needs_refresh,
                "refreshable_reasons": refreshable,
                "structural_reasons": structural,
                "suggested_sync": suggested,
            },
            "coverage": shape_coverage(coverage),
            "reasons": sorted(reasons),
            **absence,
        }
