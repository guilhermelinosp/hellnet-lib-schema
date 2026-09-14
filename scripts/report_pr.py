"""Privileged, idempotent PR report for completed read-only workflows."""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
WORKFLOWS = ("validate-schema-pr", "pr-check", "security", "codeql")
MARKER = "<!-- hellnet-actions-report -->"
CHECK_NAME = "hellnet-actions / validation"
LABELS = {
    "schema": ("1d76db", "Contract/schema changes"),
    "ci": ("5319e7", "CI configuration"),
    "github-actions": ("5319e7", "GitHub Actions changes"),
    "security": ("b60205", "Security-related changes"),
    "documentation": ("0075ca", "Documentation changes"),
    "dependencies": ("0366d6", "Dependency changes"),
    "automation": ("8250df", "Bot-generated maintenance"),
}

def api(path, method="GET", payload=None):
    request = urllib.request.Request(API + path, method=method, headers={
        "Accept": "application/vnd.github+json", "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if payload is not None:
        request.data = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        if error.code in (404, 422):
            return error.code, {}
        raise RuntimeError(f"GitHub API {error.code} for {path}") from error

def main():
    repo = os.environ["GITHUB_REPOSITORY"]
    run = json.loads(os.environ["WORKFLOW_RUN_JSON"])
    if run.get("repository", {}).get("full_name") != repo:
        return
    sha = run["head_sha"]
    _, prs = api(f"/repos/{repo}/commits/{sha}/pulls")
    if not prs:
        prs = run.get("pull_requests", [])
    if not prs:
        return
    number = prs[0]["number"]
    _, runs = api(f"/repos/{repo}/actions/runs?head_sha={urllib.parse.quote(sha)}&per_page=100")
    latest = {}
    for item in runs.get("workflow_runs", []):
        name = item.get("name")
        if name in WORKFLOWS and name not in latest:
            latest[name] = item
    statuses = {name: latest.get(name, {}).get("conclusion", "pending") for name in WORKFLOWS}
    terminal = all(value != "pending" for value in statuses.values())
    failed = any(value in {"failure", "cancelled", "timed_out", "action_required"} for value in statuses.values())
    overall = "FAIL" if failed else ("PASS" if terminal else "RUNNING")
    conclusion = "failure" if failed else ("success" if terminal else "neutral")
    _, files = api(f"/repos/{repo}/pulls/{number}/files?per_page=100")
    paths = [item["filename"] for item in files]
    wanted = {"automation"}
    if any(path.startswith("schemas/") for path in paths):
        wanted.add("schema")
    if any(path.startswith((".github/workflows/", ".github/actions/")) for path in paths):
        wanted.update({"ci", "github-actions"})
    if any(path.startswith(("SECURITY", ".github/dependabot", "scripts/security")) for path in paths):
        wanted.add("security")
    if any(path.endswith((".md", ".rst")) or path.startswith(("docs/", "CONTRIBUTING")) for path in paths):
        wanted.add("documentation")
    if "requirements.txt" in paths or ".github/dependabot.yml" in paths:
        wanted.add("dependencies")
    for label in sorted(wanted):
        color, description = LABELS[label]
        api(f"/repos/{repo}/labels/{urllib.parse.quote(label, safe='')}", "POST",
            {"name": label, "color": color, "description": description})
    api(f"/repos/{repo}/issues/{number}/labels", "POST", {"labels": sorted(wanted)})
    lines = [MARKER, "## hellnet-actions", "", *(f"{name}: {statuses[name].upper()}" for name in WORKFLOWS),
             "", f"Overall: {overall}", "", f"Commit: `{sha[:12]}`"]
    body = "\n".join(lines) + "\n"
    _, comments = api(f"/repos/{repo}/issues/{number}/comments?per_page=100")
    existing = next((item for item in comments if MARKER in item.get("body", "")), None)
    if existing:
        api(f"/repos/{repo}/issues/comments/{existing['id']}", "PATCH", {"body": body})
    else:
        api(f"/repos/{repo}/issues/{number}/comments", "POST", {"body": body})
    _, checks = api(f"/repos/{repo}/commits/{sha}/check-runs?check_name={urllib.parse.quote(CHECK_NAME)}")
    output = {"title": "hellnet-actions validation", "summary": body}
    if checks.get("check_runs"):
        check_id = checks["check_runs"][0]["id"]
        payload = {"status": "completed" if terminal else "in_progress", "output": output}
        if terminal:
            payload["conclusion"] = conclusion
        api(f"/repos/{repo}/check-runs/{check_id}", "PATCH", payload)
    else:
        payload = {"name": CHECK_NAME, "head_sha": sha, "status": "completed" if terminal else "in_progress",
                   "output": output}
        if terminal:
            payload["conclusion"] = conclusion
        api(f"/repos/{repo}/check-runs", "POST", payload)

if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
