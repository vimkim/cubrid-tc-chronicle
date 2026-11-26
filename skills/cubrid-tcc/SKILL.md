---
name: cubrid-tcc
description: Query CUBRID gha-ci testcase status, failure history, flakiness, and retained CI evidence with cubrid-tcc. Use when checking whether a testcase failed before, listing recent or per-PR failures, diagnosing suspected flakiness, syncing gha-ci runs into the local Chronicle index, or inspecting coverage gaps and retained evidence files.
---

# cubrid-tcc (CUBRID TC Chronicle)

Use `cubrid-tcc` to query retained `gha-ci` testcase observations offline and sync specific runs when local evidence is missing or stale.

## Default agent workflow

1. Start with `cubrid-tcc status <NAME> --json`.
2. If exit code is `3` (`ambiguous_identity`), re-query using one of the returned full paths, `--suite`, or `--variant` (use `--variant unknown` when variant is missing).
3. Inspect `status`, `flakiness.assessment`, `freshness`, and `coverage` independently before drawing conclusions.
4. Use `failures`, `history`, `explain`, or `evidence` for deeper investigation.
5. If `freshness.needs_refresh` is `true` and `freshness.suggested_sync` names specific `--run` IDs, run that sync once; if it names a date window (`--since`/`--until`) or `--missing`, report the coverage gap instead of blocking a normal query turn unless the user explicitly requested a backfill.

## Offline queries

All query commands read the local SQLite index (`$TCC_DATA_DIR` or `~/.local/share/cubrid-tc-chronicle`) without network access:

```bash
# 14-day status summary (verdict, counts, flakiness, freshness, coverage)
cubrid-tcc status cbrd_27461 --suite sql --json
cubrid-tcc status cbrd_27461 --pr 8753 --days 7 --json
cubrid-tcc status cbrd_27461 --branch develop --engine b74043df --json

# Fast millisecond failure listing via partial failure index
cubrid-tcc failures --pr 8753 --latest-run --json
cubrid-tcc failures --days 7 --suite shell --json
cubrid-tcc failures --run 38868725373 --json

# Resolve ambiguous testcase names
cubrid-tcc find cbrd_27461 --json

# Full retained failure history & commit grouping (optional git ancestry)
cubrid-tcc history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql --commits --json
cubrid-tcc history cbrd_27461.sql --suite sql \
  --ancestor-of b74043df --engine-repository /path/to/CUBRID --json

# Explain one failure occurrence and compare earlier failures
cubrid-tcc explain --run 38868725373 --suite sql --case cbrd_27461.sql --json

# List or print SHA-256 verified retained evidence files
cubrid-tcc evidence --execution 1 --json
cubrid-tcc evidence --execution 1 --file summary_info

# Inspect discovery & result coverage gaps
cubrid-tcc coverage --suite sql --since 2026-10-16 --until 2026-10-17 --json
```

## Syncing runs (`sync-data`)

`sync-data` reads GitHub (`gh` auth) and the internal result store (`$TCC_STORE_URL`):

```bash
# Index one specific run and suite without revisiting queued runs (~13-40s)
cubrid-tcc sync-data --run 38868725373 --suite sql --revisit-limit 0 --json

# Refresh PR titles, authors, and base/head branches on existing runs
cubrid-tcc sync-data --pr-metadata-only --json

# Drain discovered-but-unimported runs from the queue (maintenance)
cubrid-tcc sync-data --missing --suite shell --request-budget 300 --json
```

Put global `--data-dir` and `--repository` before the subcommand; `--json` works before or after.

## Supported suites

- Accepted `--suite` values: `sql`, `medium`, `shell`, and CI aliases `test_sql`, `test_medium`, `test_shell`.
- `test_sql_by_cci` (`sql_by_cci`) is **not** collected. Query commands exit `0` with `UNSUPPORTED_SUITE` (`reasons: ["suite_not_collected"]`); `sync-data` exits `2` with `unsupported_suite`. Do not substitute `sql` results for `test_sql_by_cci`.

## Interpreting verdicts, flakiness, and gaps

- **Status vs. flakiness vs. freshness**:
  - A later `PASS` never erases an earlier failure.
  - `flakiness.assessment == "SUSPECTED"` requires independent runs (or verified distinct job/attempt/shard executions) with identical `engine_sha`, `testcase_sha`, build mode, variant, CTP SHA, platform, and configuration digest. Different commits with different outcomes do not establish flakiness.
  - `PASSING` within a stale or gapped window does not prove the current engine head passes.
- **History classifications**:
  - `SEEN_BEFORE`: an earlier case-level failure exists in scope.
  - `FIRST_OBSERVED_IN_WINDOW`: no earlier failure matches and the discovery/result window is complete.
  - `UNKNOWN_HISTORY`: missing evidence or unresolved execution ordering prevents a stronger claim.
- **Why nothing came back (`not_indexed_reason`)**:
  - `suite_failures_only`: every snapshot in scope lists failures only (for example, shell evidence), so absence never proves a pass or stability.
  - `never_observed`: no indexed run of the suite contains this path.
  - `observed_outside_window`: indexed elsewhere, not in the requested filter window.
- **Output & exit codes**:
  - With `--json`, every outcome (reports, errors, ambiguity) prints one JSON object to `stdout`; progress always goes to `stderr`.
  - Exit `0`: command succeeded (including `FAILING`, `UNKNOWN`, `NOT_INDEXED`, `UNSUPPORTED_SUITE`, or empty matches).
  - Exit `2`: usage, operational, or collection error.
  - Exit `3`: ambiguous testcase identity or occurrence (`candidates` returned).
