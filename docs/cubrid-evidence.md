# CUBRID CI evidence for Chronicle

Inspected: **2026-10-16**. These observations come from CUBRID source, authenticated read-only GitHub requests, and small HTTP reads from the internal artifact server. No workflow was triggered or changed.

## Sources and revision

The local engine checkout was at `4adcd409072b189f5d965bde5750c53176d90de0`. The relevant workflow sections were also checked through GitHub at `3beb7654b489f005156066983ae6722136674e41`. Links below pin the latter revision. These revisions describe the inspected configuration, not a promise about future runs.

The [workflow suite table](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L174-L225) defines:

| Suite | Testcase repository | Published verdict source | Default split |
|---|---|---|---|
| `shell` | `cubrid-testcases-private-ex` | `test_status.data`, with individual cases in `test-shell.xml` | Individual shell cases, 50 shards |
| `sql` | `cubrid-testcases` | `summary_info`, with supporting JUnit XML | Top-level directories, 10 shards |
| `medium` | `cubrid-testcases` | `summary_info`, with supporting JUnit XML | Top-level directories, 1 shard |

The SQL and medium plan therefore names directories. A plan entry is not necessarily an individual testcase. The public repository contains separate [SQL](https://github.com/CUBRID/cubrid-testcases/tree/develop/sql) and [medium](https://github.com/CUBRID/cubrid-testcases/tree/develop/medium) trees.

## Detailed results live outside GitHub artifact storage

The configured artifact base is `http://192.168.1.48:30080`. The [workflow declares this base](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L169-L175) and [states that run directories are internal and pruned after 30 days](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L2785-L2798). The server responded from this host during inspection. The actual pruning job and effective storage policy were not audited.

The [plan publisher](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L1604-L1633) and [shard publisher](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L2026-L2119) establish this layout:

```text
runs/<run_id>/<suite>/
  plan/
    split.meta               # testcase SHA/branch and planned counts
    cases.list               # selected execution units
    plan.tsv                 # counts per shard
    shards/<index>.list      # each shard's assigned units
  shard/<index>/
    build.read               # engine SHA and identity of the build consumed
    tc.read                  # testcase SHA/branch consumed by this shard
    shard.done               # shard publication marker, not a pass verdict
    summary_info             # SQL/medium testcase verdicts
    test_status.data         # shell aggregate counts
    test-results/*.xml       # individual results and failure details
    artifacts/               # supplementary logs and failure archives
  collect/failed.list        # tab-separated shard and failed testcase path
```

The [collector](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L2540-L2576) writes complete failure lists separately from the displayed summary. The summary displays at most 50 failed paths. A missing or empty failure list alone cannot prove a testcase passed.

The runner can exit zero despite testcase failures. The workflow explicitly reads verdict files rather than trusting the CTP exit status. [Failure archive decision](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L2066-L2089).

## Real failed-test sample

The [GitHub record for run 38868725373](https://github.com/CUBRID/cubrid/actions/runs/38868725373) describes an `issue_comment` run for PR #8001. The following small files were read directly from the internal store. The paths below are evidence identifiers relative to the configured base.

| Evidence | Observed value |
|---|---|
| GitHub workflow `head_sha` | `b8199438e5fa92b77f71b0ca39004ff286daef8a` |
| `runs/38868725373/sql/shard/00/build.read`, `sha` | `b74043dfabe0ad36ee4939353a8848a939f90184` |
| Same file, build mode | `debug` |
| `runs/38868725373/sql/plan/split.meta`, `tc_sha` | `d54fe412eff5f6763245777d33139b974f4e21e0` |
| Same file, testcase branch | `tc/pr-8001` |
| Same file, selected SQL count | `17571` |
| `runs/38868725373/sql/collect/failed.list` | Shard `09`, `sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql` |
| `runs/38868725373/sql/shard/09/summary_info` | `/home/cubrid-testcases/sql/_36_guava/cbrd_27461/cases/cbrd_27461.sql:nok    163ms` |
| `runs/38868725373/sql/shard/09/tc.read` | Same testcase SHA as the plan |
| `runs/38868725373/sql/shard/09/shard.done` | `assigned=408`, `ctp_rc=0` |

The SQL shard summary contained 408 distinct leaf verdict lines, including this failure. Aggregate directory statistics also appear in the same file. The shell XML sample contained `testcase` elements with repository-relative `name` and `file` attributes. Its `classname` URL referred to `develop`, so that URL must not override the recorded testcase SHA.

This sample proves that collecting detailed outcomes and the actual tested engine SHA is feasible from this host. It does not establish this testcase's earlier history. The inspection did not archive the complete run or verify every shard's provenance.

## Rerun limitations

The workflow [sets its directory to `runs/${GITHUB_RUN_ID}`](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L1297-L1308), without an attempt suffix. The [summary explains partial GitHub reruns and separate `/run rerun` requests](https://github.com/CUBRID/cubrid/blob/3beb7654b489f005156066983ae6722136674e41/.github/workflows/gha-ci.yml#L2600-L2617).

Consequences inferred from that layout:

- A later job can replace a file at the same URL. Chronicle must retain content snapshots.
- A current directory can contain results produced by different job attempts.
- `build.read.run_attempt` identifies the build producer's attempt. A reused build does not identify the attempt that executed a testcase.
- Old overwritten outcomes cannot be recovered merely by enumerating GitHub attempts. Attempt-specific logs or a snapshot captured earlier can supply evidence when available.

A future CI manifest could remove this ambiguity. No CI change is necessary to begin indexing current evidence.

## Remaining checks before implementation acceptance

Inspect samples from failed shell and medium runs, successful runs, partial reruns, canceled shards, and older workflow generations. Measure accessible history and parser agreement with aggregate counts. CircleCI and QA-portal evidence require separate format and access investigations before those histories can be declared indexed.
