# GitHub Actions evidence constraints

Fetched: **2026-10-16**. Scope: primary GitHub documentation for CUBRID TC Chronicle. These are documented API and retention rules, not a measurement of CUBRID's effective repository settings. No workflow was triggered and no external data was changed.

## Discovering historical runs

GitHub provides repository-wide run listing and workflow-specific listing. The latter accepts a workflow filename, so `GET /repos/CUBRID/cubrid/actions/workflows/gha-ci.yml/runs` is suitable for discovery. Documented filters include event, branch, status/conclusion, creation range, and `head_sha`; there is no testcase-path filter. Pages default to 30 entries and support at most 100. Filtered searches using `actor`, `branch`, `check_suite_id`, `created`, `event`, `head_sha`, or `status` return at most 1,000 results. [Workflow runs REST API](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow)

**Implication:** split backfills into creation-time windows, subdivide windows that reach the cap, follow pagination, and deduplicate run IDs at window boundaries. Testcase search requires collected result data. Discover all relevant conclusions: filtering only current failures can omit a run that failed in an earlier attempt and later passed.

## Workflow commit versus tested commit

For `issue_comment`, GitHub defines `GITHUB_SHA` as the last commit on the default branch and `GITHUB_REF` as the default branch. For `pull_request`, the default SHA is the PR merge commit, with `github.event.pull_request.head.sha` available separately. These values describe the triggering event; a workflow can explicitly fetch another revision. [Workflow trigger events](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#issue_comment), [Pull request event](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)

**Implication:** do not equate the Actions run's `head_sha` with the engine revision tested by CUBRID's comment-triggered workflow. Preserve the trigger SHA and the tested revision separately. Resolve the latter from revision-bearing build or result evidence. Querying a PR's current head later cannot establish its head at the historical test time.

## Attempts and jobs

`GITHUB_RUN_ID` remains unchanged across reruns. `GITHUB_RUN_ATTEMPT` starts at 1 and increases with each rerun. [Actions variables](https://docs.github.com/en/actions/reference/workflows-and-actions/variables#default-environment-variables)

GitHub supports rerunning the entire workflow, failed jobs, or selected jobs. Reruns preserve the original event's `GITHUB_SHA` and `GITHUB_REF`. [Rerunning workflows and jobs](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/re-run-workflows-and-jobs)

The general jobs endpoint defaults to `filter=latest`; `filter=all` includes previous executions. An attempt-specific jobs endpoint is available, as is an individual job-log endpoint. [Workflow jobs REST API](https://docs.github.com/en/rest/actions/workflow-jobs)

| Evidence | Read endpoint, relative to `/repos/{owner}/{repo}` |
|---|---|
| Run attempt | `/actions/runs/{run_id}/attempts/{attempt_number}` |
| Attempt jobs | `/actions/runs/{run_id}/attempts/{attempt_number}/jobs` |
| Attempt log archive | `/actions/runs/{run_id}/attempts/{attempt_number}/logs` |
| Individual job log | `/actions/jobs/{job_id}/logs` |

The attempt and attempt-log endpoints are documented by the [workflow runs API](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run-attempt); job endpoints are documented by the [workflow jobs API](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt).

**Implication:** retain `(repository, run_id, run_attempt, job_id)` as provenance. A new workflow attempt is not proof that every testcase ran again. A retry inside a shell script is also not automatically a GitHub workflow attempt. External files need their own attempt attribution; an attempt-specific API does not make a shared external directory immutable.

## Evidence expiration and deletion

The current organization retention documentation says the default is 90 days. Public repositories allow 1–90 days; private repositories allow 1–400 days, subject to organization or enterprise limits. It explicitly says that, starting **2026-10-01**, the policy covers checks, workflow runs, and commit statuses as well as artifacts and logs. Changing the setting applies to new objects, not retroactively to existing objects. [GitHub retention policy](https://docs.github.com/en/organizations/managing-organization-settings/configuring-the-retention-period-for-github-actions-artifacts-and-logs-in-your-organization)

The first-party announcement also says cleanup starts on October 1, 2026 and covers third-party checks and statuses. Thus old advice that run metadata necessarily survives artifact expiration is no longer a safe design assumption. [GitHub retention announcement](https://github.blog/changelog/2026-07-17-actions-retention-will-cover-checks-workflow-runs-and-statuses/)

Artifact metadata includes `expired`, `expires_at`, ID, size, and digest. The artifact download endpoint documents HTTP `410 Gone`; its redirected URL expires after one minute. Artifacts can also be explicitly deleted. Artifact listing is scoped to a run, with no attempt-number parameter in the documented endpoint. [Artifacts REST API](https://docs.github.com/en/rest/actions/artifacts)

Attempt-log download redirects also expire after one minute, and workflow logs can be explicitly deleted. [Workflow logs REST API](https://docs.github.com/en/rest/actions/workflow-runs#download-workflow-run-attempt-logs)

**Implication:** fetch evidence promptly; keep durable source IDs, retrieval time, content hashes, and availability state. Store the API endpoint as provenance rather than a temporary signed download URL. An empty artifact list alone does not distinguish no upload, deletion, and expiration. Missing historical data must remain a coverage gap, not a passing result.

## Authentication and request budget

The run, job, and artifact read endpoints above document fine-grained token access with repository **Actions: read**. Several list/log endpoints allow unauthenticated public access, but artifact download documents token requirements. Read-only collection does not require Actions write access. [Run permissions](https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow), [Job permissions](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt), [Artifact download permissions](https://docs.github.com/en/rest/actions/artifacts#download-an-artifact)

Typical REST limits are 60 requests/hour unauthenticated, 5,000/hour for a personal token, and 1,000/hour/repository for an Actions `GITHUB_TOKEN`; some enterprise/app limits differ. Secondary limits also apply. Honor `Retry-After` and `x-ratelimit-*`; handle both `403` and `429`. [REST rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api)

GitHub recommends serialized requests and following `Link` headers for pagination. Cache `ETag` or `Last-Modified` when supplied. Correctly authenticated conditional requests returning `304` do not consume the primary rate limit. A private-resource `404` can indicate insufficient access rather than deletion. [REST best practices](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)

## Remaining verification

Before claiming complete history, measure the repository's accessible date range, effective retention, event/workflow coverage, result-format generations, and access to its external artifact store. The policy dates above are verified documentation; actual rollout, CUBRID's configured retention, and survival of individual objects were not tested here. “First observed failure within collected coverage” is defensible; “first ever failure” requires additional evidence.
