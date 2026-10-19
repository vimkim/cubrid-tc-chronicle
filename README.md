# CUBRID TC Chronicle

Check testcase status from the terminal, or browse failure history across CUBRID commits and CI runs.

Given a failed testcase, Chronicle answers:

- What are its latest observed verdict and recent failure counts?
- Is there comparable PASS and FAIL evidence that suggests flakiness?
- Which tested engine commits show this failure?
- Which PR's CI run recorded the failure, and who authored that PR?
- Did this testcase fail before the selected occurrence?
- Where can I inspect the original result and failure details?
- How much relevant history is available, missing, or not yet indexed?

Version **0.3.0** provides a CLI and a read-only web UI for GitHub Actions `gha-ci` SQL, medium, and shell evidence.

## Install and query

Use Linux with Python 3.12 or later, `uv`, `just`, an authenticated `gh`, and access to the internal CUBRID result store.

```sh
just setup

# Install tcc on your PATH. Run from the primary main checkout.
just sync

# Collect a run. This reads GitHub and the internal result store.
just sync-data --run 38868725373

# These queries use retained local evidence and work offline.
tcc status cbrd_27461 --json
just find cbrd_27461
just history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
just explain --run 38868725373 --suite sql \
  --case sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
```

`just sync` reinstalls the user-level `tcc` command with an editable uv tool installation.
Run it from the primary checkout on `main`. Use `just setup` for the development environment.
`just sync-data` collects CI evidence. The installed equivalent is `tcc sync-data`.
The old `tcc sync` command remains a collection alias and prints a migration notice.

The companion dotfiles integration registers `~/gh/cubrid-tc-chronicle` with `daily-update`.
It checks for a clean primary `main`, fetches `origin/main`, fast-forwards, and runs `just sync`.
Daily upkeep refreshes the installed CLI. Data collection remains an explicit command.

## Query a testcase for an agent

```sh
tcc status cbrd_27461.sql --suite sql --json
tcc status cbrd_27461 --pr 7925 --days 7 --json
tcc status cbrd_27461 --branch develop --engine b74043df --json
```

`status` defaults to the preceding 14 days across indexed branches. It reports the latest observed
verdict, counts, recent evidence, PR details, suspected flakiness, data age, and coverage gaps.
Names must resolve to one path, suite, and variant. Ambiguous names return candidates and exit code 3.

Read `status`, `flakiness.assessment`, and `freshness` separately. A later pass can coexist with suspected
flakiness. Different commits with different outcomes do not establish flakiness. Missing ordering or
context remains unknown. A `PASSING` observation describes retained evidence within the named scope.
Check coverage and freshness before treating it as current. The [CLI guide](docs/cli.md) defines the fields.

## Collect a window

Import a bounded UTC run-creation window, then inspect its gaps:

```sh
just sync-data --since 2026-10-16T07:39:00Z --until 2026-10-16T07:47:00Z
just coverage --suite sql --json
```

Chronicle reports **seen before**, **first observed in the selected window**, or **unknown history**. It does not infer a passing result from an absent failure entry.

The [CLI guide](docs/cli.md) covers filters, storage, replay, and error codes. The [initial pilot report](docs/pilot.md) records a real recurrence and the ten-million-observation benchmark.

## Browse the evidence

```sh
just serve
```

Open the access URL printed in the terminal. The web UI searches retained testcases, explains earlier failures, and shows outcomes across tested engine commits. Select a failure as the investigation target, filter the timeline, and open checksum-verified evidence. Coverage details remain visible alongside every history result.

The selected failure and timeline show the PR number, title, author, base branch, and head branch. PR numbers and authors link to GitHub. New collections retain these details automatically. Refresh PR details for existing history without downloading testcase results:

```sh
just sync-data --pr-metadata-only
```

An unknown PR or unavailable author remains explicit. A failure in a PR's CI run does not establish that the PR caused it.

By default, the server listens on all interfaces and prints session access URLs for the available IPv4 and IPv6 addresses. Open a LAN or VPN URL from another computer. Address discovery uses Linux's `ip` command from `iproute2`. Use `just serve-local` for loopback-only access.

Both modes read the same local SQLite index as the CLI. Collection continues through `just sync-data`; the UI's Refresh button reads the latest committed index. The [web guide](docs/web.md) covers LAN access, optional SSH forwarding, browser checks, and HTTP query behavior.

Run `just` to list the recipes. Recipes forward their arguments to the CLI. For example, use `just serve --port 8877` to select a port. Use `just tcc` for global options:

```sh
just tcc --data-dir /path/to/index history cbrd_27461
```

## Evidence and limitations

Chronicle reads the tested engine SHA from each shard's `build.read`. It retains compressed evidence by SHA-256 and imports observations atomically into SQLite. Repeated synchronization preserves earlier failures and does not duplicate an unchanged snapshot.

The current result store reuses directories across attempts. Chronicle reports unresolved attempt attribution and partial coverage. A green job, empty failure list, or missing testcase never establishes a passing outcome.

The initial pilot indexed 39,422 explicit outcomes in 72 result snapshots from four runs. It established an earlier failure of `cbrd_27461.sql` on engine `24d2542fa021…`, before the documented occurrence on `b74043dfabe0…`. The pilot covers a small window and selected runs.

The [expanded validation](docs/history-expansion.md) retains **2,518,123 explicit outcomes in 7,327 snapshots from 368 run records**. It verifies three public SQL recurrences. Discovery found 370 runs; two unavailable GitHub inventories and other source gaps remain visible.

Parser version 2 keeps missing verdicts unknown. A plan alone cannot establish `NOT_RUN`. Run `just reparse` to update retained version 1 evidence without contacting the source servers.

CircleCI, the QA portal, rename aliases, and symptom fingerprints remain future work. The detailed design includes proposals beyond this release.

## Development

```sh
just setup
just check
just build
```

Optional headless browser verification:

```sh
just browser-setup
just browser-check
```

The application has no third-party runtime dependencies. Tests use public recorded verdicts, sanitized shell specimens, and explicit replay adapters. Private evidence stays in the local data directory.

Preview documents with `docserve` installed:

```sh
just docs
just docs docs/web.md
just docs docs/history-expansion.md
just docs docs/cli.md
just docs docs/design.md
```

## Design documents

- [Project design](docs/design.md): behavior, architecture, data model, collection, and delivery plan.
- [Web guide](docs/web.md): local UI, SSH access, and browser verification.
- [CUBRID evidence](docs/cubrid-evidence.md): verified workflow layout and a real failed-test sample.
- [GitHub evidence](docs/github-evidence.md): discovery, attempts, retention, permissions, and request limits.

Design date: **2026-10-16**. Accepted project name: **CUBRID TC Chronicle**. Repository directory: `cubrid-tc-chronicle`.
