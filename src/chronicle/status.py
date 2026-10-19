"""Conservative status and flakiness policy over explicit testcase observations."""

from collections import Counter

FAILURES = {"FAIL", "ERROR"}
VERDICTS = {"PASS", *FAILURES}


def comparison_context(occurrence):
    metadata = occurrence["metadata"]
    return {
        "testcase_sha": occurrence["testcase_sha"],
        "build_mode": occurrence["build_mode"],
        **{
            key: metadata.get(key) or None
            for key in ("ctp_sha", "platform", "configuration_digest", "execution_variant")
        },
    }


def _reference(occurrence):
    return {
        "execution_id": occurrence["id"],
        **{
            key: occurrence[key]
            for key in (
                "run_id",
                "attempt",
                "job_id",
                "shard",
                "ordinal",
                "completed_at",
                "outcome",
                "engine_sha",
                "testcase_sha",
                "engine_branch",
                "pull_request",
                "source",
                "evidence",
                "evidence_digest",
                "locator",
                "parser_version",
                "manifest_digest",
            )
        },
        "run_url": occurrence["metadata"].get("run_url")
        or f"https://github.com/{occurrence['repository']}/actions/runs/{occurrence['run_id']}",
        "complete": bool(occurrence["complete"]),
    }


def _independent(left, right):
    if left["run_id"] != right["run_id"]:
        return True
    # Several fetches of a mutable result directory are not separate invocations.
    keys = ("attempt", "job_id", "shard")
    return all(left[k] is not None and right[k] is not None for k in keys) and any(
        left[k] != right[k] for k in keys
    )


def _flakiness(occurrences, limit):
    groups = {}
    reasons = set()
    verdicts = [o for o in occurrences if o["outcome"] in VERDICTS]
    for occurrence in verdicts:
        context = {"engine_sha": occurrence["engine_sha"], **comparison_context(occurrence)}
        if any(value is None for value in context.values()):
            reasons.add("comparison_context_unknown")
            continue
        if any(d["reason"] == "invocation_unresolved" for d in occurrence["diagnostics"]):
            reasons.add("invocation_unresolved")
            continue
        groups.setdefault(tuple(context.items()), []).append(occurrence)
    mixed, comparable = [], 0
    for context, group in groups.items():
        # One independent pair establishes repeat evidence for this context.
        runs = {o["run_id"] for o in group}
        invocations = {
            (o["attempt"], o["job_id"], o["shard"])
            for o in group
            if o["attempt"] is not None and o["job_id"] is not None
        }
        if len(runs) < 2 and len(invocations) < 2:
            continue
        comparable += 1
        passes = [o for o in group if o["outcome"] == "PASS"]
        failures = [o for o in group if o["outcome"] in FAILURES]
        pair = next(
            (
                (passed, failed)
                for passed in passes
                for failed in failures
                if _independent(passed, failed)
            ),
            None,
        )
        if pair:
            mixed.append(
                {
                    "context": dict(context),
                    "pass": _reference(pair[0]),
                    "failure": _reference(pair[1]),
                }
            )
    outcomes = {o["outcome"] for o in verdicts}
    mixed_outcomes = "PASS" in outcomes and bool(outcomes & FAILURES)
    if mixed_outcomes and not mixed:
        reasons.add("mixed_outcomes_do_not_establish_flakiness")
    if not comparable:
        reasons.add("insufficient_comparable_executions")
    return {
        "assessment": "SUSPECTED" if mixed else "NOT_OBSERVED" if comparable else "UNKNOWN",
        "basis": "independent_executions_with_equal_recorded_context",
        "mixed_outcomes": mixed_outcomes,
        "comparable_contexts": comparable,
        "suspect_contexts": len(mixed),
        "examples": mixed[:limit],
        "reasons": sorted(reasons),
    }


def assess(occurrences, limit):
    """Keep latest verdict, repeat evidence, and bounded source references separate."""
    counts = Counter(o["outcome"] for o in occurrences)
    unordered = sum(o["completed_at"] is None for o in occurrences)
    latest_at = max((o["completed_at"] for o in occurrences if o["completed_at"]), default=None)
    latest = [o for o in occurrences if latest_at and o["completed_at"] == latest_at]
    outcomes = {o["outcome"] for o in latest}
    reasons = []
    if unordered or not latest:
        status = "UNKNOWN"
        reasons.append("ordering_unknown" if unordered else "no_observations_in_window")
    elif "PASS" in outcomes and outcomes & FAILURES:
        status = "MIXED"
    elif outcomes & FAILURES:
        status = "FAILING"
    elif "PASS" in outcomes:
        status = "PASSING"
    else:
        status = "UNKNOWN"
        reasons.append("latest_verdict_not_executed")
    ordered = list(reversed(occurrences))
    failure = next((o for o in ordered if o["completed_at"] and o["outcome"] in FAILURES), None)
    passed = next((o for o in ordered if o["completed_at"] and o["outcome"] == "PASS"), None)
    failed_commits = sorted(
        {o["engine_sha"] for o in occurrences if o["outcome"] in FAILURES and o["engine_sha"]}
    )
    return {
        "status": status,
        "basis": "latest_observed_completion_in_window",
        "latest_observed_at": latest_at,
        "latest_known": [_reference(o) for o in latest[:limit]],
        "latest_known_count": len(latest),
        "counts": {
            outcome: counts[outcome] for outcome in ("PASS", "FAIL", "ERROR", "SKIP", "NOT_RUN")
        },
        "observations": len(occurrences),
        "snapshots": len({o["id"] for o in occurrences}),
        "runs": len({o["run_id"] for o in occurrences}),
        "failure_runs": len({o["run_id"] for o in occurrences if o["outcome"] in FAILURES}),
        "passing_runs": len({o["run_id"] for o in occurrences if o["outcome"] == "PASS"}),
        "failed_engine_commit_count": len(failed_commits),
        "failed_engine_commits": failed_commits[:limit],
        "failed_engine_commits_truncated": len(failed_commits) > limit,
        "latest_known_failure": _reference(failure) if failure else None,
        "latest_known_pass": _reference(passed) if passed else None,
        "unordered_observations": unordered,
        "flakiness": _flakiness(occurrences, limit),
        "recent": [_reference(o) for o in ordered[:limit]],
        "recent_limit": limit,
        "recent_truncated": len(occurrences) > limit,
        "reasons": reasons,
    }
