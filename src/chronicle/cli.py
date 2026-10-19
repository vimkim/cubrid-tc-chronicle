"""Argument handling and report presentation. History policy lives in query.py."""

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__
from .collection import CollectionError, Collector, GitHub, ResultStore
from .models import SUITES, Bundle
from .parsing import parse
from .query import AmbiguousIdentity, HistoryQuery, Queries, StatusQuery
from .storage import Index


def default_data_dir():
    if os.environ.get("TCC_DATA_DIR"):
        return Path(os.environ["TCC_DATA_DIR"])
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "cubrid-tc-chronicle"
    )


def parser():
    root = argparse.ArgumentParser(
        prog="tcc", description="Check CUBRID testcase status and search retained CI evidence."
    )
    root.add_argument("--version", action="version", version=f"tcc {__version__}")
    root.add_argument("--data-dir", type=Path, default=default_data_dir())
    root.add_argument("--repository", default="CUBRID/cubrid")
    root.add_argument("--json", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)

    def command(name, help_text):
        p = commands.add_parser(name, help=help_text)
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
        return p

    sync = command("sync-data", "Collect a run or a bounded UTC creation-time window")
    sync.add_argument("--run", type=int, action="append", default=[])
    sync.add_argument("--since")
    sync.add_argument("--until")
    sync.add_argument("--suite", choices=SUITES, action="append")
    sync.add_argument(
        "--store", default=os.environ.get("TCC_STORE_URL", "http://192.168.1.48:30080")
    )
    sync.add_argument("--request-budget", type=int, default=2000)
    sync.add_argument("--revisit-limit", type=int, default=20)
    sync.add_argument(
        "--pr-metadata-only",
        action="store_true",
        help="Refresh PR details for retained runs without downloading testcase results",
    )
    # Keep existing collection scripts working. Installation belongs to just sync.
    commands.add_parser("sync", parents=[sync], add_help=False, help="Legacy alias for sync-data")
    status = command("status", "Summarize recent verdicts, suspected flakiness, and freshness")
    status.add_argument("case", help="Full path, filename, or unambiguous testcase fragment")
    status.add_argument("--suite", choices=SUITES)
    status.add_argument("--variant", help="Recorded execution variant, or 'unknown'")
    status.add_argument("--branch", help="Recorded engine branch")
    status.add_argument("--engine", dest="engine_sha")
    status.add_argument("--pr", type=int)
    window = status.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="Window length (default: 14 days)")
    window.add_argument("--since", help="UTC run creation time, or date")
    status.add_argument("--until", help="UTC run creation time, or date (default: now)")
    status.add_argument(
        "--max-age-hours",
        type=float,
        default=24,
        help="Maximum discovery or TC observation age before STALE (default: 24)",
    )
    status.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum evidence examples per list, 1–100 (default: 5)",
    )
    find = command("find", "Find testcase identities without merging ambiguous names")
    find.add_argument("term")
    find.add_argument("--suite", choices=SUITES)
    find.add_argument("--variant", help="Recorded execution variant, or 'unknown'")
    for name in ("history", "explain"):
        p = command(name, "Query offline testcase observations and earlier failures")
        if name == "history":
            p.add_argument("case")
            view = p.add_mutually_exclusive_group()
            view.add_argument("--commits", action="store_true")
            view.add_argument("--runs", action="store_true")
        else:
            p.add_argument("--case", required=True)
            target = p.add_mutually_exclusive_group(required=True)
            target.add_argument("--run", type=int, dest="run_id")
            target.add_argument("--execution", type=int, dest="execution_id")
            p.add_argument("--ordinal", type=int)
        p.add_argument("--suite", choices=SUITES)
        p.add_argument("--since")
        p.add_argument("--until")
        p.add_argument("--engine", dest="engine_sha")
        p.add_argument("--branch")
        p.add_argument("--pr", type=int)
        p.add_argument("--variant", help="Recorded execution variant, or 'unknown'")
        p.add_argument("--outcome", choices=("PASS", "FAIL", "SKIP", "ERROR", "NOT_RUN"))
        p.add_argument("--ancestor-of")
        p.add_argument(
            "--engine-repository", help="Local engine Git repository for offline ancestry checks"
        )
    coverage = command("coverage", "Inspect discovery and result gaps")
    coverage.add_argument("--since")
    coverage.add_argument("--until")
    coverage.add_argument("--suite", choices=SUITES)
    command("reparse", "Verify and reparse retained snapshots without network access")
    evidence = command("evidence", "Read retained evidence for one execution")
    evidence.add_argument("--execution", type=int, required=True)
    evidence.add_argument(
        "--file", help="Print raw retained bytes for this shard-relative filename"
    )
    inspect = command("inspect", "Parse a local bundle without changing the index")
    inspect.add_argument("bundle", type=Path)
    web = command("serve", "Serve the read-only web UI on all interfaces and print access URLs")
    web.add_argument("--port", type=int, default=8765)
    web.add_argument("--local", action="store_true", help="Bind only to loopback")
    return root


def read_bundle(path: Path) -> Bundle:
    """Load an explicit local replay manifest. File paths stay inside its directory."""
    manifest = json.loads(path.read_text())
    if (
        not isinstance(manifest, dict)
        or not {"run_id", "suite", "shard", "files"} <= manifest.keys()
    ):
        raise ValueError("bundle must specify run_id, suite, shard, and files")
    if (
        not isinstance(manifest["run_id"], int)
        or manifest["run_id"] < 1
        or manifest["suite"] not in SUITES
        or not isinstance(manifest["shard"], str)
    ):
        raise ValueError("bundle identity is invalid")
    if not isinstance(manifest["files"], dict) or any(
        not isinstance(name, str) or not isinstance(relative, str)
        for name, relative in manifest["files"].items()
    ):
        raise ValueError("bundle files must map evidence names to relative filenames")
    if any(not isinstance(manifest.get(key, {}), dict) for key in ("metadata", "sources")):
        raise ValueError("bundle metadata and sources must be objects")
    if manifest.keys() - {"run_id", "suite", "shard", "files", "metadata", "sources"}:
        raise ValueError("bundle contains unsupported fields")
    parent = path.resolve().parent
    files = {}
    for name, relative in manifest["files"].items():
        source = (parent / relative).resolve()
        if not source.is_relative_to(parent):
            raise ValueError("bundle file is outside the manifest directory")
        files[name] = source.read_bytes()
    manifest["files"] = files
    return Bundle(**manifest)


def format_coverage(coverage):
    window = coverage["window"]
    print(f"Window: {window['since'] or 'unknown'} through {window['until'] or 'unknown'} UTC")
    print(
        f"Coverage: {'complete' if coverage['complete'] else 'partial'}; "
        f"{coverage['enumerated_runs']} enumerated runs; "
        f"{coverage['complete_results']} complete / {coverage['partial_results']} partial snapshots"
    )
    if coverage["reasons"]:
        print("Gaps: " + ", ".join(coverage["reasons"]))
    print(
        f"Last synchronized: {coverage['last_sync'] or 'never'}; "
        f"generation {coverage['generation']}"
    )
    print("Excluded: " + ", ".join(coverage["excluded_sources"]))
    print(coverage["upstream_limit"])


def format_pull_request(occurrence):
    pr = occurrence.get("pull_request")
    if pr is None:
        return "PR unknown; author unknown"
    author = f"@{pr['author']}" if pr.get("author") else "unknown"
    return (
        f"PR #{pr['number']}; author {author}; title: {pr.get('title') or 'unavailable'}; "
        f"base: {pr.get('base_ref') or 'unknown'}; head: {pr.get('head_ref') or 'unknown'}; "
        f"{pr['url']}"
    )


def format_history(report, commits=False):
    print(
        f"{report['history_result']}: {len(report['prior_failures'])} earlier failure snapshots; "
        f"{len(report['failed_commits'])} distinct failed engine commits in this result"
    )
    if report["identity"]:
        identity = report["identity"]
        print(
            f"Testcase: {identity['repository']} / {identity['path']}; "
            f"variant {identity['variant'] or 'unknown'}"
        )
    target = report["target"]
    if target:
        print(
            f"Target: run {target['run_id']} / snapshot {target['id']} / "
            f"{report['target_selection']}"
        )
        print("  " + format_pull_request(target))
    print(
        f"Scope: {report['scope']['branches']}; prior verified executions: "
        f"{report['prior_verified_executions']}"
    )
    if not report["prior_verified_executions"]:
        print("No prior verified executions in this result.")
    if report["reasons"]:
        print("History limits: " + ", ".join(report["reasons"]))
    format_coverage(report["coverage"])
    if commits:
        for sha in report["failed_commits"]:
            refs = [
                o
                for o in report["occurrences"]
                if o["engine_sha"] == sha and o["outcome"] in ("FAIL", "ERROR")
            ]
            print(f"{sha}  " + ", ".join(f"run {o['run_id']}/snapshot {o['id']}" for o in refs))
            for o in refs:
                print(f"  Run {o['run_id']}: {format_pull_request(o)}")
    else:
        print("Completed UTC         Engine        TC revision   Outcome  Run / snapshot")
        for o in reversed(report["occurrences"]):
            engine = (o["engine_sha"] or "unknown")[:12]
            tc = (o["testcase_sha"] or "unknown")[:12]
            print(
                f"{(o['completed_at'] or 'unknown'):21} {engine:12}  {tc:12}  "
                f"{o['outcome']:7}  {o['run_id']} / {o['id']}"
            )
            print("  " + format_pull_request(o))
            if o.get("source"):
                print("  " + o["source"])


def format_status(report):
    identity = report["identity"]
    print(
        f"{report['status']} | flaky: {report['flakiness']['assessment']} | "
        f"data: {report['freshness']['state']}"
    )
    if identity:
        print(f"Testcase: {identity['path']}; variant {identity['variant'] or 'unknown'}")
    print(f"Latest observed completion: {report['latest_observed_at'] or 'unknown'}")
    print(f"Window: {report['window']['since']} through {report['window']['until']} UTC")
    print("Scope: " + ", ".join(f"{k}={v}" for k, v in report["scope"].items() if v is not None))
    print(
        f"{report['observations']} observations in {report['runs']} runs; "
        + ", ".join(f"{k}={v}" for k, v in report["counts"].items())
    )
    for occurrence in report["latest_known"]:
        print(
            f"  {occurrence['outcome']} run {occurrence['run_id']} / "
            f"snapshot {occurrence['execution_id']} / engine "
            f"{(occurrence['engine_sha'] or 'unknown')[:12]}"
        )
        print("  " + format_pull_request(occurrence))
        print(f"  Evidence: tcc evidence --execution {occurrence['execution_id']}")
    failure = report["latest_known_failure"]
    if failure:
        print(
            f"Latest known failure: {failure['completed_at']} / run {failure['run_id']} / "
            f"snapshot {failure['execution_id']}; {report['failure_runs']} runs with failures"
        )
    flaky = report["flakiness"]
    print(
        f"Flakiness: {flaky['comparable_contexts']} comparable recorded contexts; "
        f"{flaky['suspect_contexts']} with independent PASS and FAIL/ERROR evidence."
    )
    if flaky["reasons"]:
        print("Flakiness limits: " + ", ".join(flaky["reasons"]))
    if report["reasons"]:
        print("Status limits: " + ", ".join(report["reasons"]))
    format_coverage(report["coverage"])
    if report["freshness"]["needs_refresh"]:
        print("Refresh evidence with tcc sync-data, or select an explicit --since/--until window.")


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "sync":
        print("tcc sync is a legacy alias; use tcc sync-data.", file=sys.stderr)
        args.command = "sync-data"
    try:
        if args.command == "serve":
            from .web import serve

            serve(args.data_dir, args.port, args.repository, local=args.local)
            return 0
        if args.command == "inspect":
            parsed = parse(read_bundle(args.bundle))
            result = {"schema_version": 1, **asdict(parsed)}
            result.pop("bundle")
            print(json.dumps(result, indent=2))
            return 0
        writable = args.command in ("sync-data", "reparse")
        if not writable and not (args.data_dir.expanduser() / "history.sqlite3").exists():
            raise ValueError("index is not initialized; run tcc sync-data first")
        with Index(args.data_dir, writable=writable) as index:
            queries = Queries(index, args.repository)
            if args.command == "sync-data":
                if args.request_budget < 1 or args.revisit_limit < 0:
                    raise ValueError(
                        "request budget must be positive; revisit limit must be nonnegative"
                    )
                if args.pr_metadata_only and (args.since or args.until or args.suite):
                    raise ValueError("--pr-metadata-only accepts --run, but not dates or suites")
                if not args.pr_metadata_only and not args.run and not args.since and not args.until:
                    args.since = (datetime.now(UTC) - timedelta(days=1)).isoformat()
                    args.until = datetime.now(UTC).isoformat()
                collector = Collector(
                    index,
                    GitHub(args.request_budget),
                    ResultStore(args.store),
                    repository=args.repository,
                    suites=args.suite or SUITES,
                    progress=lambda message: print(message, file=sys.stderr),
                )
                if args.pr_metadata_only:
                    collected = collector.refresh_pr_metadata(run_ids=args.run)
                else:
                    collected = collector.sync(
                        run_ids=args.run,
                        since=args.since,
                        until=args.until,
                        revisit_limit=args.revisit_limit,
                    )
                result = {"schema_version": 1, **collected}
            elif args.command == "find":
                result = {
                    "schema_version": 1,
                    "candidates": queries.find(args.term, args.suite, args.variant),
                    "coverage": queries.coverage(suite=args.suite),
                }
            elif args.command == "status":
                result = queries.status(
                    StatusQuery(
                        **{name: getattr(args, name) for name in StatusQuery.__dataclass_fields__}
                    )
                )
            elif args.command == "coverage":
                result = {
                    "schema_version": 1,
                    **queries.coverage(args.since, args.until, args.suite),
                }
            elif args.command == "reparse":
                result = {"schema_version": 1, **index.reparse()}
            elif args.command == "evidence":
                bundle = index.bundle(args.execution)
                if args.file:
                    if args.file not in bundle.files:
                        raise ValueError(f"file not retained: {args.file}")
                    sys.stdout.buffer.write(bundle.files[args.file])
                    return 0
                result = {
                    "schema_version": 1,
                    "run_id": bundle.run_id,
                    "suite": bundle.suite,
                    "shard": bundle.shard,
                    "metadata": bundle.metadata,
                    "files": {
                        k: {"bytes": len(v), "source": bundle.sources.get(k)}
                        for k, v in bundle.files.items()
                    },
                }
            else:
                query_args = {
                    name: getattr(args, name)
                    for name in HistoryQuery.__dataclass_fields__
                    if hasattr(args, name)
                }
                result = queries.history(HistoryQuery(**query_args))
            if args.json or args.command in ("evidence", "reparse"):
                print(json.dumps(result, indent=2))
            elif args.command in ("history", "explain"):
                format_history(result, getattr(args, "commits", False))
            elif args.command == "status":
                format_status(result)
            elif args.command == "coverage":
                format_coverage(result)
            elif args.command == "find":
                for c in result["candidates"]:
                    print(f"{c['suite']:6} {c['path']}")
                if not result["candidates"]:
                    print("No indexed testcase matches.")
                format_coverage(result["coverage"])
            elif args.command == "sync-data" and args.pr_metadata_only:
                print(
                    f"Refreshed PR metadata for {result['runs']} retained runs; "
                    f"{result['linked_runs']} runs linked to a PR."
                )
                for failure in result["failures"]:
                    print(f"Run {failure['run_id']}: {failure['reason']}", file=sys.stderr)
            else:
                print(
                    f"Synchronized {result['runs']} runs; "
                    f"{result['inserted_snapshots']} new snapshots; "
                    f"{result['observations']} new outcomes; "
                    f"{result['partial_snapshots']} partial snapshots; "
                    f"{result['unavailable_files']} unavailable files."
                )
                for failure in result["failures"]:
                    print(f"Run {failure['run_id']}: {failure['reason']}", file=sys.stderr)
            return 2 if result.get("failures") else 0
    except AmbiguousIdentity as e:
        error = {
            "schema_version": 1,
            "error": "ambiguous_identity",
            "message": str(e),
            "candidates": e.candidates,
        }
        print(
            json.dumps(error, indent=2)
            if args.json
            else str(e) + "\n" + "\n".join(str(c) for c in e.candidates),
            file=sys.stderr,
        )
        return 3
    except (ValueError, OverflowError, OSError, sqlite3.Error, CollectionError) as e:
        error = {"schema_version": 1, "error": "operational_error", "message": str(e)}
        print(json.dumps(error) if args.json else f"tcc: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
