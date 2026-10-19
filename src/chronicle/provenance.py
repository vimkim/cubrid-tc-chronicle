"""PR associations from retained run evidence, independent of current PR heads."""

import re


def run_pull_request(run):
    """Read the run's PR association without using actors or testcase branches."""
    if not run:
        return None
    numbers = set()
    association = None
    if run.get("event") in ("issue_comment", "workflow_dispatch"):
        # CUBRID gha-ci's run-name starts with the triggering/requested PR.
        for key in ("name", "display_title"):
            match = re.match(r"^PR #([1-9][0-9]*) · ", run.get(key) or "")
            if match:
                numbers.add(int(match[1]))
        association = "run_title"
    elif run.get("event") in ("pull_request", "pull_request_target"):
        numbers = {p["number"] for p in run.get("pull_requests", []) if p.get("number")}
        association = "run_pull_requests"
    if len(numbers) != 1:
        return None
    number = numbers.pop()
    repository = run["repository"]
    result = {
        "number": number,
        "url": f"https://github.com/{repository}/pull/{number}",
        "title": None,
        "base_ref": None,
        "head_ref": None,
        "author": None,
        "author_url": None,
        "association": association,
    }
    retained = run.get("chronicle_pr", {})
    if retained.get("number") == number:
        result.update(retained)
    return result
