# Chronicle 0.1 pilot

Date: **2026-10-16**. Host: Linux x86_64. Application Python: **3.14.0**. Compatibility tests also ran on **3.12.9**.

## Live recurrence

The pilot collected a closed creation-time interval from **07:39:00 through 07:47:00 UTC**, plus the earlier SQL run **38848190241**. The interval enumerated three runs: **38868643765**, **38868725373**, and **38869372662**. It includes a selected shell case and a build-only run, in addition to the full three-suite run.

The active index contains **39,422 outcomes in 72 shard snapshots from four runs**: 20 SQL, one medium, and 51 shell snapshots. Earlier development parse records remain archived but do not enter current queries.

The query below returns `SEEN_BEFORE` for `sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql`:

```sh
uv run cubrid-tcc explain --run 38868725373 --suite sql \
  --case sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql
```

| Job completion UTC | Run | Tested engine SHA | Testcase revision | Outcome |
|---|---|---|---|---|
| 2026-10-16 04:43:10 | [38848190241](https://github.com/CUBRID/cubrid/actions/runs/38848190241) | `24d2542fa021194b76114d93a856184e87bae07a` | `7bf9ed984e3bc5d41fa400d10f996d1c577614e8` | FAIL |
| 2026-10-16 08:05:02 | [38868725373](https://github.com/CUBRID/cubrid/actions/runs/38868725373) | `b74043dfabe0ad36ee4939353a8848a939f90184` | `d54fe412eff5f6763245777d33139b974f4e21e0` | FAIL |

Each outcome comes from the shard's `summary_info`, with the engine SHA from `build.read` and testcase SHA from `tc.read`. Original locations are under the configured internal store:

```text
runs/38848190241/sql/shard/09/{summary_info,build.read,tc.read}
runs/38868725373/sql/shard/09/{summary_info,build.read,tc.read}
```

The target run's workflow SHA is `b8199438e5fa92b77f71b0ca39004ff286daef8a`. Chronicle correctly excludes that trigger SHA from failed engine commit counts. The testcase revision differs between occurrences. Historical engine branches and full execution context remain unknown. These observations establish testcase recurrence, without establishing a common cause or an introducing commit.

## Gaps and retained evidence

Both full SQL collections contain a root-counter discrepancy in shard `01`. In the target run, the plan and leaf verdicts count **3,533** cases, while the root counter reports **3,531**. Chronicle retains the explicit verdicts and records `count_mismatch`. The active pilot has 70 complete result snapshots and two partial result snapshots.

All live snapshot attempts remain unresolved because the store reuses run directories. The bounded interval has complete GitHub discovery, but result coverage remains partial. The earlier manually selected run lies outside that interval. Default coverage therefore reports an incomplete discovery window across all retained runs.

The target run's first import produced **21,855 outcomes in 61 shards**. Repeating the collection produced **zero new snapshots and zero new outcomes**. Offline reparse also produced zero new snapshots. The development index occupied about **17.7 MB** for SQLite and **0.67 MB** for compressed objects. The database retains superseded development parse rows, so these sizes do not establish production bytes per outcome.

Raw private shell evidence remains outside the repository. Checked-in shell fixtures preserve the recorded XML structure with replaced paths, removed failure bodies, recomputed counters, and synthetic provenance. Public SQL and medium fixtures retain their recorded verdict bytes. [Fixture provenance](../tests/fixtures/provenance.json) records checksums and identifies these transformations.

The automated suite covers partial reruns and incomplete shards through replay. A real overwritten workflow-attempt reconstruction was not established. The collector preserves that uncertainty instead of assigning current files to every attempt.

## Ten-million-observation benchmark

```sh
uv run python scripts/benchmark.py --observations 10000000
```

The measurement used a temporary corpus with **10,000 testcases × 1,000 executions**, producing **10,000,000 observations**. The script exercised the read-only history interface and returned 1,000 occurrences for one testcase. It retained shared recorded raw evidence, so this measurement covers database/query behavior. It does not measure production archive growth or network collection speed.

| Measurement | Result |
|---|---|
| Corpus construction | 81.061 seconds |
| SQLite size | 1,046,851,584 bytes |
| Five history queries | 73.742, 77.014, 74.072, 77.889, 69.584 ms |
| One-second target | All five queries passed |

The temporary corpus was removed after measurement. The first measured query used a freshly opened SQLite connection. This was not a controlled cold-filesystem benchmark, and concurrent production workload was not simulated.

The [recorded benchmark result](benchmark-20261016.json) preserves the numeric output.

## Release validation

Validation includes the replay suite on Python 3.12 and 3.14, Ruff checks, package build, offline queries, reparse, and live collection. Repeated live collection checks idempotence. Recorded responses test pagination and window splitting. The replay suite checks atomic rollback, writer exclusion, reader snapshots, ambiguous identities, wrong trigger SHA, missing provenance, inconsistent counts, and history classifications.

The release provides manual collection and offline queries. CircleCI, QA history, scheduling, deployment, a web UI, rename aliases, and symptom fingerprints remain outside this pilot.

Markdown source checks passed. The existing design diagram did not change. Mermaid revalidation could not run because the local viewer's Mermaid bundle was unavailable.
