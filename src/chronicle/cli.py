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
from .query import AmbiguousIdentity, HistoryQuery, Queries
from .storage import Index


def default_data_dir():
    if os.environ.get("TCC_DATA_DIR"):
        return Path(os.environ["TCC_DATA_DIR"])
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / "cubrid-tc-chronicle"
    )


def parser():
    root = argparse.ArgumentParser(
        prog="tcc", description="Search retained CUBRID testcase evidence."
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

    sync = command("sync", "Collect a run or a bounded UTC creation-time window")
    sync.add_argument("--run", type=int, action="append", default=[])
    sync.add_argument("--since")
    sync.add_argument("--until")
    sync.add_argument("--suite", choices=SUITES, action="append")
    sync.add_argument(
        "--store", default=os.environ.get("TCC_STORE_URL", "http://192.168.1.48:30080")
    )
    sync.add_argument("--request-budget", type=int, default=2000)
    sync.add_argument("--revisit-limit", type=int, default=20)
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
    else:
        print("Completed UTC         Engine        TC revision   Outcome  Run / snapshot")
        for o in reversed(report["occurrences"]):
            engine = (o["engine_sha"] or "unknown")[:12]
            tc = (o["testcase_sha"] or "unknown")[:12]
            print(
                f"{(o['completed_at'] or 'unknown'):21} {engine:12}  {tc:12}  "
                f"{o['outcome']:7}  {o['run_id']} / {o['id']}"
            )
            if o.get("source"):
                print("  " + o["source"])


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "inspect":
            parsed = parse(read_bundle(args.bundle))
            result = {"schema_version": 1, **asdict(parsed)}
            result.pop("bundle")
            print(json.dumps(result, indent=2))
            return 0
        writable = args.command in ("sync", "reparse")
        if not writable and not (args.data_dir.expanduser() / "history.sqlite3").exists():
            raise ValueError("index is not initialized; run tcc sync first")
        with Index(args.data_dir, writable=writable) as index:
            queries = Queries(index, args.repository)
            if args.command == "sync":
                if args.request_budget < 1 or args.revisit_limit < 0:
                    raise ValueError(
                        "request budget must be positive; revisit limit must be nonnegative"
                    )
                if not args.run and not args.since and not args.until:
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
                result = {
                    "schema_version": 1,
                    **collector.sync(
                        run_ids=args.run,
                        since=args.since,
                        until=args.until,
                        revisit_limit=args.revisit_limit,
                    ),
                }
            elif args.command == "find":
                result = {
                    "schema_version": 1,
                    "candidates": queries.find(args.term, args.suite, args.variant),
                    "coverage": queries.coverage(suite=args.suite),
                }
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
            elif args.command == "coverage":
                format_coverage(result)
            elif args.command == "find":
                for c in result["candidates"]:
                    print(f"{c['suite']:6} {c['path']}")
                if not result["candidates"]:
                    print("No indexed testcase matches.")
                format_coverage(result["coverage"])
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
    except (ValueError, OSError, sqlite3.Error, CollectionError) as e:
        error = {"schema_version": 1, "error": "operational_error", "message": str(e)}
        print(json.dumps(error) if args.json else f"tcc: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
