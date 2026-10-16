# Chronicle CLI guide

Version: **0.1.0**. Supported host: Linux with Python 3.12 or later.

## Collection

Run commands from the project checkout after `uv sync --locked`. GitHub reads use the existing `gh` authentication. The default internal result store is `http://192.168.1.48:30080`.

```sh
uv run tcc sync --run 38868725373
uv run tcc sync --run 38848190241 --suite sql
uv run tcc sync --since 2026-10-15 --until 2026-10-16 --suite sql --json
```

Date-only bounds include each complete UTC day. Timestamp bounds include both endpoints, at one-second precision. Discovery uses **run creation time**. Query date filters use the same basis. Testcase execution order uses recorded job completion time when the evidence supports that ordering.

Without a run or window, `sync` discovers the preceding 24 hours. Every synchronization also checks up to 20 due runs from the persistent revisit queue. Use `--revisit-limit 0` to collect only the explicit selection. New runs receive a second check after 15 minutes. Completed runs then remain due for checks every 12 hours. In-progress runs remain due every 15 minutes. These are queue deadlines. A manual `sync` performs the checks. Chronicle does not install a scheduler.

Chronicle serializes GitHub requests. Discovery follows pages and splits windows before the filtered 1,000-run cap. Changed counts or duplicate page entries prevent complete discovery coverage. `--request-budget 2000` bounds GitHub requests. Rate-limit responses stop collection and preserve any `Retry-After` value in the error. Retry a failed synchronization to resume retained progress.

Each run import enumerates every workflow attempt and its jobs. Internal result reads use at most six threads. A second read detects files that change during collection. Such snapshots retain their verdicts but lose verified revision attribution. A stable fetch cannot establish an attempt number.

Chronicle collects small provenance, plan, and verdict files. Each HTTP response has a 16 MiB limit. An unavailable store or rejected response remains a coverage gap. A `404` means `access_denied_or_missing`, without an expiration claim. SQL failure details in supplementary XML and large core/debug archives remain outside this release.

## Offline queries

```sh
uv run tcc find cbrd_27461
uv run tcc history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql --commits
uv run tcc explain --run 38868725373 --suite sql \
  --case sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql --json
```

`find` lists candidate identities. `history` accepts a full path, basename, suffix, or ticket fragment. Ambiguous identities require a full path, suite, or variant. Case spelling remains significant for identity. Fragment search ignores case.

`history` anchors its summary to the latest ordered indexed failure. `explain` selects a failure in a particular run. If several failure snapshots match that run, select `--execution ID` instead. `--ordinal N` resolves repeated case records inside a snapshot. A passing retry remains a separate observation and does not remove an earlier failure.

History filters include `--suite`, `--since`, `--until`, `--engine SHA_PREFIX`, `--branch`, `--pr`, `--outcome`, and `--variant`. Use `--variant unknown` for records without a known variant. Recorded engine references determine branch and PR matching. A workflow branch or testcase branch cannot substitute for an unknown historical engine reference. Current `issue_comment` evidence commonly lacks those engine references.

An optional ancestry filter uses a local engine Git checkout:

```sh
uv run tcc history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql \
  --ancestor-of b74043dfabe0ad36ee4939353a8848a939f90184 \
  --engine-repository /path/to/CUBRID --json
```

Missing commits or an unavailable Git checkout produce unknown ancestry. Queries do not fetch Git objects. Earlier failures include comparison fields for testcase revision, tooling, build mode, platform, configuration, and variant. Missing fields remain unknown. Mixed pass/fail outcomes establish a possible intermittent failure only when every recorded context field agrees.

## Classification and coverage

| Result | Meaning |
|---|---|
| `SEEN_BEFORE` | An earlier case-level failure exists in the selected scope |
| `FIRST_OBSERVED_IN_WINDOW` | No earlier failure matches, and the selected discovery and result window is complete |
| `UNKNOWN_HISTORY` | Missing evidence or uncertain order prevents the stronger conclusion |

The current shared result directories do not encode testcase execution attempts. The collector therefore leaves attempt and job attribution unresolved. A unique completed job in a run with one attempt can supply ordering. It does not supply a verified attempt identity. Live results usually retain `UNKNOWN_HISTORY` when no earlier failure is found. Replay tests exercise all three classifications with explicit provenance.

The timeline counts **retained result snapshots**. A mutable shard can produce several snapshots, and unresolved invocation identity can prevent an exact execution count. Distinct failed commit counts exclude observations without a verified engine SHA.

```sh
uv run tcc coverage --since 2026-10-16T07:39:00Z \
  --until 2026-10-16T07:47:00Z --suite sql --json
```

Coverage reports discovery, missing runs and shards, individual result gaps, unresolved revisions and attempts, earliest retained execution, and synchronization time. Default coverage spans the retained run metadata and discovery windows. A narrow discovery interval does not silently cover manually imported runs outside it. A complete window still cannot detect every upstream run removed before initial indexing.

## Storage and replay

The default data directory is `$XDG_DATA_HOME/cubrid-tc-chronicle`, or `~/.local/share/cubrid-tc-chronicle` when that variable is absent. Set `TCC_DATA_DIR` or use the global `--data-dir` option to select another index. Set `TCC_STORE_URL` or `sync --store URL` to change the result store. Global `--repository OWNER/REPO` selects the GitHub repository.

```sh
uv run tcc --data-dir /path/to/private-index sync --run 38868725373
uv run tcc evidence --execution 1 --json
uv run tcc evidence --execution 1 --file summary_info
uv run tcc reparse --json
uv run tcc inspect tests/fixtures/sql-recorded/bundle.json
```

Snapshot IDs come from query output and belong to one local index. `evidence` checks retained object hashes before printing raw bytes. JSON history includes the original URL, evidence digest, locator, parser version, and manifest digest. The original URL can change after a rerun. The retained object preserves the bytes used by the parser.

New data directories use mode `0700`. New database, archive, and writer-lock files use mode `0600`. Existing directory permissions remain unchanged. Keep private shell evidence in this restricted directory.

The database uses SQLite WAL. One process holds the writer lock. Queries use read-only connections and a consistent transaction snapshot. Imports publish an execution atomically after archive verification. Interrupted imports can leave unreferenced compressed objects, but cannot publish half an execution. `reparse` verifies every active bundle before it atomically selects the new parse generation. Older evidence remains retained.

JSON reports use `schema_version: 1`. Command exit codes are:

| Code | Meaning |
|---|---|
| `0` | Successful query or collection, including empty matches and explicitly partial result coverage |
| `2` | Operational error, invalid arguments, incomplete discovery, or failed GitHub run collection |
| `3` | Ambiguous testcase identity or target occurrence |

Inspect `coverage` after collection. An exit code of zero does not establish complete historical coverage.
