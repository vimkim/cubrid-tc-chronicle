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


class HelpParser(argparse.ArgumentParser):
    def error(self, message):
        super().error(f"{message}\nRun '{self.prog} --help' for examples.")


def parser():
    root = HelpParser(
        prog="cubrid-tcc",
        description="Check CUBRID testcase status and search retained CI evidence.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""Examples:
  cubrid-tcc status cbrd_27461 --json
  cubrid-tcc find cbrd_27461 --json
  cubrid-tcc history cbrd_27461 --suite sql --json
  cubrid-tcc sync-data --run 38868725373 --revisit-limit 0

For agents:
  Start with status NAME --json. Queries read the local index offline.
  If the index is missing or stale, collect evidence with sync-data.
  sync-data reads GitHub and the result store, then writes the local index.
  JSON reports use stdout. Errors and collection progress use stderr.
  Exit 0: command succeeded, including FAILING, UNKNOWN, or empty query results.
  Exit 2: invalid arguments or an operational or collection error.
  Exit 3: ambiguous testcase or occurrence. Select a returned candidate.
  Check freshness and coverage before drawing conclusions from a verdict.
  Put --data-dir and --repository before the command. --json works on either side.
  Run cubrid-tcc COMMAND --help for command details and examples.""",
    )
    root.add_argument("--version", action="version", version=f"cubrid-tcc {__version__}")
    root.add_argument(
        "--data-dir",
        type=Path,
        default=default_data_dir(),
        help="Local index directory (default: %(default)s). "
        "Uses TCC_DATA_DIR or XDG_DATA_HOME when set",
    )
    root.add_argument(
        "--repository", default="CUBRID/cubrid", help="GitHub OWNER/REPO (default: CUBRID/cubrid)"
    )
    json_help = "Print a JSON report for agents and scripts"
    root.add_argument("--json", action="store_true", help=json_help)
    commands = root.add_subparsers(
        dest="command", required=True, title="commands", metavar="COMMAND"
    )

    def command(name, help_text, description, *examples, notes=""):
        epilog = "Examples:\n" + "\n".join(f"  cubrid-tcc {example}" for example in examples)
        if notes:
            epilog += "\n\n" + notes
        p = commands.add_parser(
            name,
            help=help_text,
            description=description,
            epilog=epilog,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help=json_help)
        return p

    since_help = "Earliest run creation time: YYYY-MM-DD or ISO 8601 timestamp (UTC)"
    until_help = "Latest run creation time (UTC). A date includes the whole day"
    case_help = "Full path, filename, or unambiguous testcase fragment"
    sync = command(
        "sync-data",
        "Collect a run or a bounded UTC creation-time window",
        "Read GitHub and the result store, then update the local index.\n"
        "Collect runs created in the past 24 hours by default.",
        "sync-data --run 38868725373 --revisit-limit 0",
        "sync-data --since 2026-10-15 --until 2026-10-16 --suite sql --json",
        "sync-data --pr-metadata-only",
        notes="GitHub access requires an authenticated gh command.\n"
        "Date selection requires both --since and --until.\n"
        "Collection also checks due retained runs unless --revisit-limit is 0.\n"
        "With --pr-metadata-only, omit --run to refresh every retained run.\n"
        "Use coverage --json afterward to inspect missing evidence.",
    )
    sync.add_argument(
        "--run",
        type=int,
        action="append",
        default=[],
        help="GitHub Actions run ID. Repeat for more runs",
    )
    sync.add_argument("--since", help=since_help)
    sync.add_argument("--until", help=until_help)
    sync.add_argument(
        "--suite",
        choices=SUITES,
        action="append",
        help="Collect this suite (default: all). Repeat for more",
    )
    sync.add_argument(
        "--store",
        default=os.environ.get("TCC_STORE_URL", "http://192.168.1.48:30080"),
        help="Result store URL (default: TCC_STORE_URL or http://192.168.1.48:30080)",
    )
    sync.add_argument(
        "--request-budget", type=int, default=2000, help="Maximum GitHub requests (default: 2000)"
    )
    sync.add_argument(
        "--revisit-limit",
        type=int,
        default=20,
        help="Maximum due retained runs to check in addition to the selection (default: 20)",
    )
    sync.add_argument(
        "--pr-metadata-only",
        action="store_true",
        help="Refresh PR details without testcase downloads. Accepts --run, no dates or suites",
    )
    # Keep existing collection scripts working. Installation belongs to just sync.
    commands.add_parser(
        "sync",
        parents=[sync],
        add_help=False,
        help="Legacy alias for sync-data",
        description=sync.description,
        epilog=sync.epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    status = command(
        "status",
        "Summarize recent verdicts, suspected flakiness, and freshness",
        "Read the local index offline.\n"
        "Report the latest verdict, counts, suspected flakiness, freshness, and coverage.\n"
        "The default window covers the past 14 days.",
        "status cbrd_27461 --json",
        "status cbrd_27461.sql --suite sql --variant unknown --json",
        "status cbrd_27461 --pr 7925 --days 7 --json",
        notes="Read status, flakiness.assessment, freshness, and coverage separately.\n"
        "Exit 0 means the query succeeded. It does not mean the testcase passed.\n"
        "For ambiguous names, use find NAME --json and select a path, suite, or variant.",
    )
    status.add_argument("case", help=case_help)
    status.add_argument("--suite", choices=SUITES, help="Select one testcase suite")
    status.add_argument("--variant", help="Recorded execution variant, or 'unknown'")
    status.add_argument("--branch", help="Recorded engine branch")
    status.add_argument("--engine", dest="engine_sha", help="Recorded engine commit SHA or prefix")
    status.add_argument("--pr", type=int, help="Retained pull request number")
    window = status.add_mutually_exclusive_group()
    window.add_argument("--days", type=int, help="Window length (default: 14 days)")
    window.add_argument("--since", help=since_help)
    status.add_argument("--until", help=f"{until_help} (default: now)")
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
    find = command(
        "find",
        "Find testcase identities without merging ambiguous names",
        "Search retained testcase paths offline by ticket, filename, or path fragment.\n"
        "Return candidate paths, suites, and variants without combining identities.",
        "find cbrd_27461 --json",
        "find cbrd_27461.sql --suite sql --json",
        notes="Use a returned path, suite, and variant with status, history, or explain.",
    )
    find.add_argument("term", help="Ticket, filename, or path fragment (search ignores case)")
    find.add_argument("--suite", choices=SUITES, help="Select one testcase suite")
    find.add_argument("--variant", help="Recorded execution variant, or 'unknown'")
    for name in ("history", "explain"):
        if name == "history":
            p = command(
                name,
                "Query offline testcase observations and earlier failures",
                "Read retained observations offline.\n"
                "Compare earlier failures with the latest ordered failure in the selected scope.\n"
                "No date window applies by default.",
                "history cbrd_27461 --json",
                "history cbrd_27461 --suite sql --commits",
                "history cbrd_27461 --branch develop --outcome FAIL --json",
                notes="To select a failure in one run or snapshot, use explain.\n"
                "--commits and --runs change text output only. JSON always includes occurrences.",
            )
            p.add_argument("case", help=case_help)
            view = p.add_mutually_exclusive_group()
            view.add_argument(
                "--commits", action="store_true", help="Group text output by failed engine commit"
            )
            view.add_argument(
                "--runs", action="store_true", help="List observations by run (default text view)"
            )
        else:
            p = command(
                name,
                "Explain one retained failure and compare earlier failures offline",
                "Select a retained failure with --case and exactly one of --run or --execution.\n"
                "Compare it with earlier retained failures without network access.",
                "explain --run 38868725373 --case cbrd_27461 --suite sql --json",
                "explain --execution 1 --case cbrd_27461 --json",
                notes="Use execution IDs from query output in the same local index.\n"
                "If a run matches several failures, select a returned --execution ID.\n"
                "If a snapshot repeats a case, also select its returned --ordinal.",
            )
            p.add_argument("--case", required=True, help=case_help)
            target = p.add_mutually_exclusive_group(required=True)
            target.add_argument("--run", type=int, dest="run_id", help="GitHub Actions run ID")
            target.add_argument(
                "--execution", type=int, dest="execution_id", help="Local retained snapshot ID"
            )
            p.add_argument(
                "--ordinal", type=int, help="Observation ordinal from an ambiguity candidate"
            )
        p.add_argument("--suite", choices=SUITES, help="Select one testcase suite")
        p.add_argument("--since", help=since_help)
        p.add_argument("--until", help=until_help)
        p.add_argument("--engine", dest="engine_sha", help="Recorded engine commit SHA or prefix")
        p.add_argument("--branch", help="Recorded engine branch")
        p.add_argument("--pr", type=int, help="Retained pull request number")
        p.add_argument("--variant", help="Recorded execution variant, or 'unknown'")
        p.add_argument(
            "--outcome",
            choices=("PASS", "FAIL", "SKIP", "ERROR", "NOT_RUN"),
            help="Keep this recorded verdict",
        )
        p.add_argument(
            "--ancestor-of", help="Keep engine commits that are ancestors of this commit"
        )
        p.add_argument(
            "--engine-repository", help="Local engine Git repository for offline ancestry checks"
        )
    coverage = command(
        "coverage",
        "Inspect discovery and result gaps",
        "Read local coverage offline.\nReport missing discovery, runs, results, and provenance.\n"
        "Without dates, inspect the retained discovery and run window.",
        "coverage --json",
        "coverage --since 2026-10-15 --until 2026-10-16 --suite sql --json",
    )
    coverage.add_argument("--since", help=since_help)
    coverage.add_argument("--until", help=until_help)
    coverage.add_argument("--suite", choices=SUITES, help="Select one testcase suite")
    command(
        "reparse",
        "Verify and reparse retained snapshots without network access",
        "Check retained evidence hashes.\nUpdate the local index with the current parser.\n"
        "This command writes the index without network access.",
        "reparse --json",
    )
    evidence = command(
        "evidence",
        "Read retained evidence for one execution",
        "Read one local snapshot offline.\nWithout --file, list retained filenames as JSON.\n"
        "With --file, print the retained bytes after hash verification.",
        "evidence --execution 1 --json",
        "evidence --execution 1 --file summary_info",
        notes="Use an execution ID from query output in the same local index.\n"
        "Select --file from the returned files. --file prints raw bytes even with --json.",
    )
    evidence.add_argument("--execution", type=int, required=True, help="Local retained snapshot ID")
    evidence.add_argument(
        "--file", help="Print raw retained bytes for this shard-relative filename"
    )
    inspect = command(
        "inspect",
        "Parse a local bundle without changing the index",
        "Parse a local bundle.json manifest and its evidence files offline.\n"
        "Print JSON without changing the index.\nNo initialized index is required.",
        "inspect tests/fixtures/sql-recorded/bundle.json",
        notes="The example uses a fixture from the project checkout.",
    )
    inspect.add_argument("bundle", type=Path, help="Path to a local bundle.json replay manifest")
    web = command(
        "serve",
        "Serve the read-only web UI on all interfaces and print access URLs",
        "Serve retained evidence in a read-only web UI.\nListen on all interfaces by default.\n"
        "Print access URLs without opening a browser.",
        "serve --local",
        "serve --port 8877",
        notes="This command runs until interrupted. Use sync-data to collect new evidence.\n"
        "--json does not change server output.",
    )
    web.add_argument("--port", type=int, default=8765, help="HTTP listening port (default: 8765)")
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
        print(f"  Evidence: cubrid-tcc evidence --execution {occurrence['execution_id']}")
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
        print(
            "Refresh evidence with cubrid-tcc sync-data, "
            "or select an explicit --since/--until window."
        )


def main(argv=None):
    root = parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv:
        root.print_help()
        return 0
    args = root.parse_args(argv)
    if args.command == "sync":
        print("cubrid-tcc sync is a legacy alias; use cubrid-tcc sync-data.", file=sys.stderr)
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
            raise ValueError("index is not initialized; run cubrid-tcc sync-data first")
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
        print(json.dumps(error) if args.json else f"cubrid-tcc: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
