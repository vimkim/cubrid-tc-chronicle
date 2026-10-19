# CUBRID TC Chronicle

Search testcase failure history across CUBRID commits and CI runs.

Given a failed testcase, Chronicle answers:

- Which tested engine commits show this failure?
- Which PR's CI run recorded the failure, and who authored that PR?
- Did this testcase fail before the selected occurrence?
- Where can I inspect the original result and failure details?
- How much relevant history is available, missing, or not yet indexed?

Version **0.2.0** provides a CLI and a read-only web UI for GitHub Actions `gha-ci` SQL, medium, and shell evidence.

## Install and query

Use Linux with Python 3.12 or later, `uv`, `just`, an authenticated `gh`, and access to the internal CUBRID result store.

```sh
just setup

# Collect a run. This reads GitHub and the internal result store.
just sync --run 38868725373

# These queries use retained local evidence and work offline.
just find cbrd_27461
just history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
just explain --run 38868725373 --suite sql \
  --case sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
```

Import a bounded UTC run-creation window, then inspect its gaps:

```sh
just sync --since 2026-10-16T07:39:00Z --until 2026-10-16T07:47:00Z
just coverage --suite sql --json
```

Chronicle reports **seen before**, **first observed in the selected window**, or **unknown history**. It does not infer a passing result from an absent failure entry.

The [CLI guide](docs/cli.md) covers filters, storage, replay, and error codes. The [initial pilot report](docs/pilot.md) records a real recurrence and the ten-million-observation benchmark.

## Browse the evidence

```sh
just serve
```

Open the access URL printed in the terminal. The web UI searches retained testcases, explains earlier failures, and shows outcomes across tested engine commits. Select a failure as the investigation target, filter the timeline, and open checksum-verified evidence. Coverage details remain visible alongside every history result.

The selected failure and timeline show the associated PR and its author, with links to both. New collections retain these details automatically. Refresh PR details for existing history without downloading testcase results:

```sh
just sync --pr-metadata-only
```

An unknown PR or unavailable author remains explicit. A failure in a PR's CI run does not establish that the PR caused it.

By default, the server listens on all interfaces and prints session access URLs for the available IPv4 and IPv6 addresses. Open a LAN or VPN URL from another computer. Address discovery uses Linux's `ip` command from `iproute2`. Use `just serve-local` for loopback-only access.

Both modes read the same local SQLite index as the CLI. Collection continues through `just sync`; the UI's Refresh button reads the latest committed index. The [web guide](docs/web.md) covers LAN access, optional SSH forwarding, browser checks, and HTTP query behavior.

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
