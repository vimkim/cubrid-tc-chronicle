"""Read-only GitHub discovery and CUBRID's retained result-store snapshots."""

import json
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from .models import SUITES, Bundle
from .parsing import parse
from .provenance import run_pull_request
from .storage import Index, encode, now

MAX_FILE_BYTES = 16 * 1024 * 1024


class CollectionError(Exception):
    pass


class RequestLimitError(CollectionError):
    pass


def timestamp(value: str, *, end: bool = False) -> str:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"invalid UTC date or timestamp: {value}") from e
    if len(value) == 10 and end:
        result += timedelta(days=1, seconds=-1)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class GitHub:
    """Uses the existing gh credentials. Requests are serialized and bounded."""

    def __init__(self, request_budget=2000):
        self.remaining = request_budget

    def get(self, endpoint):
        if self.remaining <= 0:
            raise RequestLimitError(
                "GitHub request budget exhausted; synchronization is incomplete"
            )
        self.remaining -= 1
        try:
            process = subprocess.run(
                ["gh", "api", "--include", endpoint, "-H", "Accept: application/vnd.github+json"],
                capture_output=True,
                timeout=45,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            raise CollectionError(
                "GitHub request failed; check gh authentication and connectivity"
            ) from e
        if process.returncode:
            # gh's error text may contain URLs or authentication information.
            match = re.search(rb"HTTP (\d{3})", process.stderr)
            status = match[1].decode() if match else "unknown"
            if status == "429" or b"rate limit" in process.stderr.lower():
                retry = re.search(rb"(?im)^retry-after:\s*([0-9]+)", process.stdout)
                delay = f"; Retry-After {retry[1].decode()} seconds" if retry else ""
                raise RequestLimitError(
                    f"GitHub rate limit reached{delay}; synchronization stopped"
                )
            raise CollectionError(
                f"GitHub request failed (HTTP {status}); retry later or check access"
            )
        try:
            _, body = re.split(rb"\r?\n\r?\n", process.stdout, maxsplit=1)
            return json.loads(body)
        except (ValueError, UnicodeError) as e:
            raise CollectionError("GitHub returned invalid JSON") from e

    def pages(self, endpoint, key):
        result = []
        separator = "&" if "?" in endpoint else "?"
        page = 1
        while True:
            body = self.get(f"{endpoint}{separator}per_page=100&page={page}")
            items = body[key]
            result.extend(items)
            if len(items) < 100:
                if len(result) < body.get("total_count", 0):
                    raise CollectionError("GitHub pagination ended before total_count")
                if len({item["id"] for item in result}) != len(result):
                    raise CollectionError("GitHub returned duplicate jobs during pagination")
                return result
            page += 1

    def discover(self, repository, since, until):
        """Split closed, second-resolution windows before GitHub's 1,000-run cap."""
        endpoint = f"repos/{repository}/actions/workflows/gha-ci.yml/runs"

        def window(lo, hi):
            query = urllib.parse.urlencode({"created": f"{lo}..{hi}", "per_page": 100})
            first = self.get(f"{endpoint}?{query}&page=1")
            if first["total_count"] >= 1000:
                a = datetime.fromisoformat(lo.replace("Z", "+00:00"))
                b = datetime.fromisoformat(hi.replace("Z", "+00:00"))
                seconds = int((b - a).total_seconds())
                if seconds < 1:
                    raise CollectionError("discovery cap reached within one second")
                middle = a + timedelta(seconds=seconds // 2)
                right = middle + timedelta(seconds=1)
                return window(lo, timestamp(middle.isoformat())) + window(
                    timestamp(right.isoformat()), hi
                )
            items = first["workflow_runs"]
            expected = first["total_count"]
            page = 2
            while len(items) < expected:
                body = self.get(f"{endpoint}?{query}&page={page}")
                if body["total_count"] != expected:
                    raise CollectionError(
                        "run discovery changed during pagination; retry the window"
                    )
                if not body["workflow_runs"]:
                    raise CollectionError("run discovery pagination ended early")
                items.extend(body["workflow_runs"])
                page += 1
            if len({r["id"] for r in items}) != expected:
                raise CollectionError("run discovery count disagrees with unique run IDs")
            return items

        return list({r["id"]: r for r in window(since, until)}.values())


class ResultStore:
    def __init__(self, base: str):
        parsed = urllib.parse.urlsplit(base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
            raise ValueError("store URL must use HTTP(S) without embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("store URL must not contain query parameters or a fragment")
        self.base = base.rstrip("/")

    def url(self, path):
        return self.base + "/" + path

    def fetch(self, path):
        try:
            request = urllib.request.Request(
                self.url(path), headers={"User-Agent": "cubrid-tcc/0.1"}
            )
            with urllib.request.urlopen(request, timeout=12) as response:
                data = response.read(MAX_FILE_BYTES + 1)
                if len(data) > MAX_FILE_BYTES:
                    return None, "size_limit_exceeded"
                # Some reverse proxies send a login/error page with HTTP 200.
                if data.lstrip().lower().startswith((b"<!doctype html", b"<html")):
                    return None, "access_denied_or_missing"
                return data, None
        except urllib.error.HTTPError as e:
            return None, "access_denied_or_missing" if e.code in (
                401,
                403,
                404,
            ) else f"http_{e.code}"
        except (urllib.error.URLError, TimeoutError, OSError):
            return None, "source_unreachable"


class Collector:
    def __init__(
        self,
        index: Index,
        github=None,
        store=None,
        *,
        repository="CUBRID/cubrid",
        suites=SUITES,
        progress=None,
    ):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid GitHub repository")
        self.index = index
        self.github = github or GitHub()
        self.store = store or ResultStore("http://192.168.1.48:30080")
        self.repository = repository
        self.suites = tuple(suites)
        self.progress = progress or (lambda _: None)

    def _reset_pr_requests(self):
        self._pr_cache = {}
        self._pr_failures = []
        self._pr_limited = False

    def _enrich_pr(self, run):
        pr = run_pull_request(run)
        if pr is None:
            return
        number = pr["number"]
        if "chronicle_pr" not in run:
            previous = self.index.db.execute(
                "SELECT metadata FROM run WHERE repository=? AND run_id=?",
                (self.repository, run["id"]),
            ).fetchone()
            retained = run_pull_request(json.loads(previous[0])) if previous else None
            if retained and retained["number"] == number:
                pr = {**retained, "association": pr["association"]}
        if number not in self._pr_cache:
            try:
                self.progress(f"Reading PR #{number} details")
                response = self.github.get(f"repos/{self.repository}/pulls/{number}")
                if response.get("number") != number:
                    raise CollectionError("GitHub returned a different PR number")
                user = response.get("user") or {}
                self._pr_cache[number] = {
                    "title": response.get("title"),
                    "base_ref": (response.get("base") or {}).get("ref"),
                    "head_ref": (response.get("head") or {}).get("ref"),
                    "author": user.get("login"),
                    "author_url": user.get("html_url"),
                    "metadata_digest": self.index.archive.put(encode(response)),
                    "checked_at": now(),
                }
            except CollectionError as e:
                self._pr_cache[number] = {"error": str(e)}
                self._pr_limited = isinstance(e, RequestLimitError)
        details = self._pr_cache[number]
        pr.pop("error", None)
        run["chronicle_pr"] = {**pr, **details}
        if "error" in details:
            self._pr_failures.append(
                {"run_id": run["id"], "reason": f"PR #{number} metadata: {details['error']}"}
            )

    def refresh_pr_metadata(self, *, run_ids=()):
        """Enrich retained runs without discovering runs or reading result files."""
        self._reset_pr_requests()
        rows = {
            r["run_id"]: r
            for r in self.index.db.execute(
                "SELECT run_id,metadata,revisit_at FROM run WHERE repository=? ORDER BY run_id",
                (self.repository,),
            )
        }
        selected = list(dict.fromkeys(run_ids)) if run_ids else list(rows)
        refreshed, linked, failures = 0, 0, []
        for run_id in selected:
            row = rows.get(run_id)
            if row is None:
                failures.append({"run_id": run_id, "reason": "run metadata is not indexed"})
                continue
            run = json.loads(row["metadata"])
            self._enrich_pr(run)
            self.index.record_run(run, row["revisit_at"])
            refreshed += 1
            linked += run_pull_request(run) is not None
            if self._pr_limited:
                break
        return {
            "runs": refreshed,
            "linked_runs": linked,
            "failures": failures + self._pr_failures,
        }

    def _fetch(self, run_id, suite, relative):
        path = f"runs/{run_id}/{suite}/{relative}"
        data, reason = self.store.fetch(path)
        self.index.availability(self.repository, run_id, suite, relative, reason)
        return data, reason

    def _run(self, run_id):
        self.progress(f"Collecting run {run_id}")
        run = self.github.get(f"repos/{self.repository}/actions/runs/{run_id}")
        if run.get("path", "").split("@", 1)[0] != ".github/workflows/gha-ci.yml":
            raise CollectionError(f"run {run_id} is not gha-ci")
        jobs = []
        for attempt in range(1, run["run_attempt"] + 1):
            jobs.extend(
                self.github.pages(
                    f"repos/{self.repository}/actions/runs/{run_id}/attempts/{attempt}/jobs", "jobs"
                )
            )
        run["repository"] = self.repository
        run["jobs"] = jobs
        # PR details describe the run association. Its current head is never
        # evidence of the historical engine revision in build.read.
        self._enrich_pr(run)
        delay = timedelta(minutes=15) if run["status"] != "completed" else timedelta(hours=12)
        # Completed runs are checked again promptly, then remain in the revisit queue.
        previous = self.index.db.execute(
            "SELECT 1 FROM run WHERE repository=? AND run_id=?", (self.repository, run_id)
        ).fetchone()
        if not previous:
            delay = timedelta(minutes=15)
        run_digest = self.index.record_run(run, timestamp((datetime.now(UTC) + delay).isoformat()))
        receipts = []
        for suite in self.suites:
            suite_jobs = [j for j in jobs if re.fullmatch(rf"shard {suite} [0-9]+", j["name"])]
            if not suite_jobs:
                # Failed gates/plan jobs cannot establish that a suite was never selected.
                plan_jobs = [j for j in jobs if j["name"] == "plan"]
                safe_no_execution = run["status"] == "completed" and (
                    run["conclusion"] == "skipped"
                    or (bool(plan_jobs) and all(j["conclusion"] == "skipped" for j in plan_jobs))
                )
                self.index.availability(
                    self.repository,
                    run_id,
                    suite,
                    "suite",
                    None if safe_no_execution else "execution_inventory_unknown",
                )
                continue
            self.index.availability(self.repository, run_id, suite, "suite", None)
            common = {}
            unreachable = False
            for name in ("plan/plan.tsv", "plan/split.meta", "collect/failed.list"):
                data, reason = self._fetch(run_id, suite, name)
                if reason == "source_unreachable":
                    unreachable = True
                    break
                if data is not None:
                    common[name] = data
            if unreachable:
                continue
            planned = {}
            try:
                for line in common.get("plan/plan.tsv", b"").decode().splitlines():
                    shard, count, *_ = line.split("\t")
                    if not re.fullmatch(r"[0-9]{2,}", shard) or int(count) < 0:
                        raise ValueError()
                    if shard in planned:
                        raise ValueError()
                    planned[shard] = str(int(count)).encode()
            except (ValueError, UnicodeError):
                self.index.availability(
                    self.repository, run_id, suite, "plan/plan.tsv", "parse_unsupported"
                )
                planned = {}
            job_shards = {j["name"].rsplit(" ", 1)[1] for j in suite_jobs}
            shards = sorted(set(planned) | job_shards)
            for shard in shards:
                names = ["build.read", "tc.read", "shard.done"]
                names += (
                    ["test_status.data", "test-results/test-shell.xml"]
                    if suite == "shell"
                    else ["summary_info"]
                )
                paths = [f"shard/{shard}/{n}" for n in names]
                paths.append(f"plan/shards/{shard}.list")
                # Internal result reads are independent. GitHub requests remain serialized.
                with ThreadPoolExecutor(max_workers=6) as pool:
                    fetched = list(
                        pool.map(self.store.fetch, [f"runs/{run_id}/{suite}/{p}" for p in paths])
                    )
                # Detect publication or replacement during collection. Stable bytes
                # still cannot establish a workflow attempt.
                with ThreadPoolExecutor(max_workers=6) as pool:
                    checked = list(
                        pool.map(
                            self.store.fetch, [f"runs/{run_id}/{suite}/{p}" for p in paths[:-1]]
                        )
                    )
                stable = all(a == b for a, b in zip(fetched[:-1], checked, strict=True))
                files, sources = {}, {}
                for path, (data, reason) in zip(paths, fetched, strict=True):
                    self.index.availability(self.repository, run_id, suite, path, reason)
                    if data is None:
                        continue
                    name = "plan/shard.list" if path.startswith("plan/") else path.split("/", 2)[2]
                    files[name] = data
                    sources[name] = self.store.url(f"runs/{run_id}/{suite}/{path}")
                for name, data in common.items():
                    key = "failed.list" if name.startswith("collect/") else name
                    files[key] = data
                    sources[key] = self.store.url(f"runs/{run_id}/{suite}/{name}")
                if shard in planned:
                    files["plan/expected"] = planned[shard]
                    sources["plan/expected"] = self.store.url(
                        f"runs/{run_id}/{suite}/plan/plan.tsv"
                    )
                matching = [
                    j
                    for j in suite_jobs
                    if j["name"] == f"shard {suite} {shard}" and j.get("conclusion") != "skipped"
                ]
                metadata = {
                    "repository": self.repository,
                    "run_url": run["html_url"],
                    "workflow_sha": run["head_sha"],
                    "event": run["event"],
                    "workflow_branch": run["head_branch"],
                    "run_attempts": run["run_attempt"],
                    "attribution": "unresolved",
                    "ordering": "unknown",
                    "retrieved_at": now(),
                    "github_metadata_digest": run_digest,
                    "fetch_consistency": "stable" if stable else "changed_during_fetch",
                }
                # A unique completed job supplies ordering for a never-rerun run.
                # The shared directory still does not encode an execution attempt.
                if stable and run["run_attempt"] == 1 and len(matching) == 1:
                    job = matching[0]
                    if job.get("completed_at") and job.get("status") == "completed":
                        metadata.update(
                            completed_at=job["completed_at"],
                            ordering="unique_job",
                            candidate_job_id=job["id"],
                            attribution="partial",
                        )
                if run["event"] == "push":
                    metadata["push_head_sha"] = run["head_sha"]
                    metadata["push_branch"] = run["head_branch"]
                parsed = parse(Bundle(run_id, suite, shard, files, metadata, sources))
                receipts.append(self.index.ingest(parsed))
        return receipts

    def sync(self, *, run_ids=(), since=None, until=None, revisit_limit=20):
        self._reset_pr_requests()
        if (since is None) != (until is None):
            raise ValueError("provide both --since and --until")
        runs = list(run_ids)
        if since is not None:
            since, until = timestamp(since), timestamp(until, end=True)
            if since > until:
                raise ValueError("--since must not be later than --until")
            discovered = []
            try:
                discovered = self.github.discover(self.repository, since, until)
            except CollectionError as e:
                self.index.record_discovery(
                    self.repository, since, until, discovered, False, str(e)
                )
                raise
            self.index.record_discovery(self.repository, since, until, discovered, True)
            runs.extend(r["id"] for r in discovered)
        if revisit_limit:
            runs.extend(
                r[0]
                for r in self.index.db.execute(
                    "SELECT run_id FROM run WHERE repository=? AND revisit_at<=? "
                    "ORDER BY revisit_at LIMIT ?",
                    (self.repository, now(), revisit_limit),
                )
            )
        receipts, failures = [], []
        for run_id in dict.fromkeys(runs):
            try:
                receipts.extend(self._run(run_id))
            except CollectionError as e:
                self.index.availability(
                    self.repository, run_id, "*", "github_metadata", "not_indexed"
                )
                failures.append({"run_id": run_id, "reason": str(e)})
                if isinstance(e, RequestLimitError):
                    break
                continue
            self.index.availability(self.repository, run_id, "*", "github_metadata", None)
            if self._pr_limited:
                break
        with self.index.db:
            self.index.db.execute("INSERT OR REPLACE INTO meta VALUES ('last_sync',?)", (now(),))
            self.index._advance()
        return {
            "runs": len(set(runs)),
            "inserted_snapshots": sum(r["inserted"] for r in receipts),
            "observations": sum(r["observations"] for r in receipts),
            "failures": failures + self._pr_failures,
            "partial_snapshots": sum(not r.get("complete", True) for r in receipts),
            "unavailable_files": sum(
                r[0] in set(runs)
                for r in self.index.db.execute(
                    "SELECT run_id FROM availability WHERE repository=?", (self.repository,)
                )
            ),
        }
