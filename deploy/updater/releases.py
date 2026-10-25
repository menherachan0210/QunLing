"""Select only commits accepted by the repository's complete CI workflow."""
import json
import re
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def get_json(url):
    request = Request(url, headers={"Accept": "application/vnd.github+json",
                                  "User-Agent": "QunLing-updater"})
    with urlopen(request, timeout=30) as response:
        return json.loads(response.read(2 * 1024 * 1024))


def verified_revision(repository, branch, fetch=get_json):
    root = "https://api.github.com/repos/" + repository
    runs = fetch(root + "/actions/workflows/ci.yml/runs?" + urlencode({
        "branch": branch, "status": "success", "per_page": 30,
    })).get("workflow_runs", [])
    for run in runs:
        revision = run.get("head_sha", "")
        if (run.get("event") not in {"push", "workflow_dispatch"}
                or run.get("head_branch") != branch
                or run.get("conclusion") != "success"
                or run.get("head_repository", {}).get("full_name", "").lower() != repository.lower()
                or not re.fullmatch(r"[0-9a-f]{40}", revision)
                or type(run.get("id")) is not int):
            continue
        jobs = fetch(root + f'/actions/runs/{run["id"]}/jobs?per_page=100').get("jobs", [])
        required = {"Public source and setup tests", "Linux image and fresh initialization"}
        passed = {job.get("name") for job in jobs if job.get("conclusion") == "success"}
        if required <= passed:
            return revision
    raise RuntimeError("没有找到公共测试和 Linux 镜像验收均通过的版本。")
