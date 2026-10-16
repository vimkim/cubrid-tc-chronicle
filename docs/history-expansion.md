# Chronicle 0.2 expanded history validation

Date: **2026-10-16**. Application: **0.2.0**, parser **2**. Host: Linux x86_64. Runtime: Python **3.14.0**; compatibility checks also pass on **3.12.9**.

## Collection and retained outcomes

The bounded creation-time interval is **2026-10-14 00:00:00 through 2026-10-16 11:25:00 UTC**. GitHub discovery enumerated **370** `gha-ci` runs. The index retains metadata for **368** of them and **2,518,123 explicit outcomes** in **7,327 shard snapshots**. The initial four-run pilot remains part of this interval.

| Suite | Retained outcomes | PASS | FAIL | SKIP | Snapshots |
|---|---|---|---|---|---|
| SQL | 2,036,500 | 2,034,737 | 1,763 | 0 | 1,186 |
| Medium | 112,120 | 112,020 | 100 | 0 | 115 |
| Shell | 369,503 | 365,511 | 635 | 3,357 | 6,026 |

The index contains **21,993 observed testcase identities**, including **1,732 with failure evidence**, across **77 distinct tested engine SHAs with failures**. Suite-specific commit counts overlap and must not be added. Snapshots preserve revisions of result files; these totals do not establish an exact number of independent executions.

Collection resumed from the unfinished run inventory after the collector update. Targeted retries recovered two GitHub HTTP 502 failures. The inventories for runs **38500611664** and **38644093509** still returned 502 after repeated attempts and remain `not_indexed`. Their discovery records remain retained. The initially executing run **38889243506** was refreshed after completion, preserving earlier snapshots.

The window and replay can be requested with:

```sh
uv run tcc sync --since 2026-10-14T00:00:00Z --until 2026-10-16T11:25:00Z \
  --request-budget 1400 --revisit-limit 0
uv run tcc reparse
uv run tcc coverage --since 2026-10-14T00:00:00Z --until 2026-10-16T11:25:00Z --json
```

Upstream results can change after reruns. These commands do not promise reproduction of the recorded byte set.

## Verified public SQL recurrences

Each selected history returns **`SEEN_BEFORE`**. The audit read the archived `summary_info` verdict at its recorded line, verified its SHA-256 digest, and checked the tested engine SHA in `build.read` and testcase SHA in `tc.read`.

| Testcase | Outcome snapshots | Failed engine SHAs | Earlier failure snapshots |
|---|---|---|---|
| `sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql` | 37 | 5 | 4 |
| `sql/_13_issues/_26_2h/cases/cbrd_27596.sql` | 64 | 12 | 11 |
| `sql/_13_issues/_26_2h/cases/cbrd_27624.sql` | 81 | 12 | 11 |

Representative ordered endpoints are:

| Testcase | Endpoint | Run | Engine SHA prefix | Testcase SHA prefix |
|---|---|---|---|---|
| `cbrd_27461.sql` | Earliest ordered failure | [38690982337](https://github.com/CUBRID/cubrid/actions/runs/38690982337) | `cbd466a2d380` | `60fb767bcc87` |
| `cbrd_27461.sql` | Latest ordered failure | [38868725373](https://github.com/CUBRID/cubrid/actions/runs/38868725373) | `b74043dfabe0` | `d54fe412eff5` |
| `cbrd_27596.sql` and `cbrd_27624.sql` | Earliest ordered failure | [38680302759](https://github.com/CUBRID/cubrid/actions/runs/38680302759) | `c0a9d2ff3d00` | `59c86e3adee0` |
| `cbrd_27596.sql` and `cbrd_27624.sql` | Latest ordered failure | [38862915270](https://github.com/CUBRID/cubrid/actions/runs/38862915270) | `2b07f612a81e` | `3182bdc8ee1b` |

The [numeric validation record](history-expansion-20261016.json) contains full SHAs, timestamps, source URLs, retained digests, and line locators. Those sources establish testcase recurrence. They do not establish a shared cause, an introducing commit, or flakiness. Testcase revisions and execution context differ or remain unknown. The earliest ordered failure in each selected history correctly returns **`UNKNOWN_HISTORY`**, because the available evidence cannot establish novelty.

```sh
uv run tcc history sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql --suite sql --variant unknown
uv run tcc history sql/_13_issues/_26_2h/cases/cbrd_27596.sql --suite sql --variant unknown
uv run tcc history sql/_13_issues/_26_2h/cases/cbrd_27624.sql --suite sql --variant unknown
```

## Parser and coverage corrections

Parser version 1 incorrectly created **10,050 `NOT_RUN` observations** from shell selection plans without retained verdicts. Version 2 keeps those outcomes unknown and records incomplete evidence. The offline replay verified the source objects of all **7,327 active snapshots** and inserted **3,437 current parse records**. All active snapshots now use parser 2. Older parses and raw evidence remain retained. The active corpus contains no plan-only `NOT_RUN` observations.

The live refresh also exposed a coverage freshness error: a newer completed run could appear stale against an older discovery snapshot. Coverage now compares source update times before applying that status disagreement. The regression test covers the transition from live discovery to newer completed metadata. Newer discovered attempts still invalidate older indexed metadata.

Discovery is complete for the selected interval; result and provenance coverage remain partial:

- **6,739** complete result snapshots and **588** partial result snapshots.
- **7,327** unresolved testcase execution attempts and **347** snapshots without established ordering.
- **296** source-file availability records marked `access_denied_or_missing`.
- **149** suite-inventory gaps, the two unavailable GitHub inventories, and one file exceeding the **16 MiB** retention limit.
- Counter mismatches, truncated or unsupported XML, missing provenance, runner errors, and failure-list-only evidence remain explicit diagnostics. Diagnostic categories overlap.

All indexed run metadata now reports completed status. A complete result snapshot does not resolve its execution attempt. CircleCI, the QA portal, and unit-test history remain outside the collected scope.

Private shell testcase paths, failure bodies, and raw files remain outside this repository. The report contains aggregate shell counts and public SQL examples.

## Web UI and verification

The [web guide](web.md) explains startup, SSH forwarding, and the HTTP interface. [The captured UI](assets/web-ui.png) shows the verified SQL recurrence and the two remaining unindexed runs.

Headless Chromium passed desktop and mobile checks for search, suite selection, failure-only and all-outcome searches, target selection, browser Back and Forward, date and outcome filters, retained evidence, coverage dialogs, saved URLs, empty search, and page width. No JavaScript errors were recorded. Additional checks passed for ambiguous testcase selection and refreshing after an index is initialized while the server is running. Each check stopped its own browser and temporary server.

All **60 Python tests** pass on Python 3.12 and 3.14. Ruff, JavaScript syntax, static formatting, package build, installed-wheel HTTP resources, and Markdown source checks pass. Docserve's Mermaid 12.2.0 checker also rendered the design diagram successfully in both light and dark themes. HTTP tests prove that history policy matches the CLI and that new requests see collection's committed snapshots.

The numeric record includes five query timings per operation. Measurements use one read-only connection and warm filesystem caches; they are not a controlled cold-cache benchmark. Failure-only browsing starts from rare failure verdicts. All-outcome path searches narrow testcase identities before reading their outcomes. Unfiltered all-outcome browsing remains slower.

| Query | Median | Maximum |
|---|---|---|
| Failure-only catalog | 12.531 ms | 13.974 ms |
| All-outcome search for `cbrd_27461` | 18.720 ms | 19.761 ms |
| `cbrd_27461` history | 803.328 ms | 1,003.342 ms |
| Overview | 2,363.955 ms | 2,417.519 ms |
| Unfiltered all-outcome catalog | 5,580.109 ms | 5,634.958 ms |

One history measurement exceeded the one-second target by 3.342 ms. The median met that target. These production-corpus measurements complement the earlier synthetic ten-million-observation benchmark; they do not provide a latency guarantee.

The SQLite file occupies **767,655,936 bytes** and the **27,205 compressed objects** occupy **52,092,359 logical bytes**. These include superseded development and parser records. They do not establish production bytes per independent execution.
