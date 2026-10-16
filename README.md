# CUBRID TC Chronicle

Search testcase failure history across CUBRID commits and CI runs.

Given a failed testcase, Chronicle answers:

- Which tested engine commits show this failure?
- Did this testcase fail before the selected occurrence?
- Where can I inspect the original result and failure details?
- How much relevant history is available, missing, or not yet indexed?

Version **0.1.0** provides a local CLI for GitHub Actions `gha-ci` SQL, medium, and shell evidence. A web UI remains future work.

## Install and query

Use Linux with Python 3.12 or later, `uv`, an authenticated `gh`, and access to the internal CUBRID result store.

```sh
uv sync --locked

# Collect a run. This reads GitHub and the internal result store.
uv run tcc sync --run 38868725373

# These queries use retained local evidence and work offline.
uv run tcc find cbrd_27461
uv run tcc history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
uv run tcc explain --run 38868725373 --suite sql \
  --case sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
```

Import a bounded UTC run-creation window, then inspect its gaps:

```sh
uv run tcc sync --since 2026-10-16T07:39:00Z --until 2026-10-16T07:47:00Z
uv run tcc coverage --suite sql --json
```

Chronicle reports **seen before**, **first observed in the selected window**, or **unknown history**. It does not infer a passing result from an absent failure entry.

The [CLI guide](docs/cli.md) covers filters, storage, replay, and error codes. The [pilot report](docs/pilot.md) records a real recurrence and the ten-million-observation benchmark.

## Evidence and limitations

Chronicle reads the tested engine SHA from each shard's `build.read`. It retains compressed evidence by SHA-256 and imports observations atomically into SQLite. Repeated synchronization preserves earlier failures and does not duplicate an unchanged snapshot.

The current result store reuses directories across attempts. Chronicle reports unresolved attempt attribution and partial coverage. A green job, empty failure list, or missing testcase never establishes a passing outcome.

The initial pilot indexed 39,422 explicit outcomes in 72 result snapshots from four runs. It established an earlier failure of `cbrd_27461.sql` on engine `24d2542fa021…`, before the documented occurrence on `b74043dfabe0…`. The pilot covers a small window and selected runs.

CircleCI, the QA portal, a web UI, rename aliases, and symptom fingerprints remain future work. The detailed design includes proposals beyond this release.

## Development

```sh
uv sync --locked --group dev
uv run ruff check src tests scripts
uv run ruff format --check src tests scripts
uv run python -m unittest discover -s tests
uv build
```

The application has no third-party runtime dependencies. Tests use public recorded verdicts, sanitized shell specimens, and explicit replay adapters. Private evidence stays in the local data directory.

## Design documents

- [Project design](docs/design.md): behavior, architecture, data model, collection, and delivery plan.
- [CUBRID evidence](docs/cubrid-evidence.md): verified workflow layout and a real failed-test sample.
- [GitHub evidence](docs/github-evidence.md): discovery, attempts, retention, permissions, and request limits.

Design date: **2026-10-16**. Accepted project name: **CUBRID TC Chronicle**. Repository directory: `cubrid-tc-chronicle`.
