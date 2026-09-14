"""Idempotent Issue -> schema branch -> reviewed PR (never merges)."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from contracts import generate, require, validate_tree
from evolution import check_evolution, protect_history

API = "https://api.github.com"


def run(*args):
    return subprocess.check_output(list(args), text=True).strip()


def api_request(path, method="GET", payload=None):
    request = urllib.request.Request(API + path, method=method, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {os.environ['GH_TOKEN']}",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    if payload is not None:
        request.data = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")
        raise RuntimeError(f"GitHub API {error.code} for {path}: {detail[:200]}") from error


def create_verified_commit(repo, branch, base, files, message, app_slug):
    """Create the generated commit through GitHub APIs, never git commit."""
    require(bool(os.environ.get("GH_TOKEN")), "GH_TOKEN is required for bot commits")
    require(bool(re.fullmatch(r"[A-Za-z0-9-]+", app_slug)), "invalid bot identity")
    base_tree = api_request(f"/repos/{repo}/git/commits/{base}")["tree"]["sha"]
    entries = []
    for path in files:
        relative = Path(path)
        require(relative.is_file() and relative.parts[0] == "schemas", "generated file is outside schemas")
        blob = api_request(f"/repos/{repo}/git/blobs", "POST", {
            "content": relative.read_text(), "encoding": "utf-8",
        })
        entries.append({"path": relative.as_posix(), "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree = api_request(f"/repos/{repo}/git/trees", "POST", {"base_tree": base_tree, "tree": entries})
    commit = api_request(f"/repos/{repo}/git/commits", "POST", {
        "message": message, "tree": tree["sha"], "parents": [base],
    })
    commit_sha = commit["sha"]
    api_request(f"/repos/{repo}/git/refs", "POST", {"ref": f"refs/heads/{branch}", "sha": commit_sha})
    verified = api_request(f"/repos/{repo}/commits/{commit_sha}")["commit"]["verification"]["verified"]
    require(verified is True, f"GitHub did not verify generated commit {commit_sha}")
    print(f"Generated commit {commit_sha} verified=true by {app_slug}[bot]")
    return commit_sha


def linked_pr(prs, number):
    marker = f"<!-- schema-issue:{number} -->"
    matches = [pr for pr in prs if marker in (pr.get("body") or "") or re.search(
        rf"(?im)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#{number}\b", pr.get("body") or "")]
    require(len(matches) <= 1, "multiple PRs reference this Issue; resolve ambiguity before retrying")
    if not matches:
        return None
    pr = matches[0]
    require(pr["state"] != "CLOSED", "existing PR was closed without merge; reopen it or create a new Issue")
    return pr


def process(repo, number, app_slug, user_id):
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo)), "invalid repository")
    require(number > 0, "Issue number must be positive")
    issue = json.loads(run("gh", "issue", "view", str(number), "--repo", repo,
                           "--json", "number,body,labels,state"))
    require(any(label["name"] == "schema" for label in issue["labels"]), "Issue must have the schema label")
    branch = f"schema/issue-{number}"
    # Head lookup does not depend on GitHub's eventually-consistent search index.
    prs = json.loads(run("gh", "pr", "list", "--repo", repo, "--state", "all", "--head", branch,
                         "--json", "number,body,state,url,headRefName"))
    if prs:
        require(linked_pr(prs, number) is not None, "existing branch PR no longer references this Issue")
    else:
        # Backward compatibility with schema/{name}-vN branches.
        prs = json.loads(run("gh", "pr", "list", "--repo", repo, "--state", "all", "--limit", "100",
                             "--search", f"in:body #{number}", "--json", "number,body,state,url,headRefName"))
        require(len(prs) < 100, "too many matching PRs to safely resolve this Issue")
    existing = linked_pr(prs, number)
    if existing:
        print(f"Already processed ({existing['state']}): {existing['url']}")
        return existing["url"]
    require(issue["state"] == "OPEN", "closed Issue has no linked PR; reopen it before retrying")
    require(not run("git", "status", "--porcelain"), "checkout must be clean")
    run("git", "fetch", "origin", "main")
    base = run("git", "rev-parse", "FETCH_HEAD")
    remote = run("git", "ls-remote", "--heads", "origin", f"refs/heads/{branch}")
    if remote:
        run("git", "fetch", "origin", f"refs/heads/{branch}")
        # Resume only an additive schema-only branch, never overwrite a review.
        run("git", "checkout", "-b", branch, "FETCH_HEAD")
        changes = run("git", "diff", "--name-status", "--no-renames", base, "HEAD").splitlines()
        require(bool(changes) and all(line.startswith("A\tschemas/") for line in changes),
                "existing branch is not schema-only/additive; inspect it manually")
    else:
        run("git", "checkout", "-b", branch, base)
        generate(issue["body"])
    validate_tree(Path("schemas"))
    protect_history(Path.cwd(), base)
    check_evolution(Path("schemas"))
    if not remote:
        message = f"feat(schema): generate contract from Issue #{number}"
        if os.environ.get("GH_TOKEN"):
            changes = run("git", "diff", "--name-status", "--no-renames", base).splitlines()
            paths = [line.split("\t", 1)[1] for line in changes if line.startswith("A\t")]
            require(paths and all(path.startswith("schemas/") for path in paths),
                    "generated commit must contain only new schema files")
            create_verified_commit(repo, branch, base, paths, message, app_slug)
        else:
            require(bool(re.fullmatch(r"[A-Za-z0-9-]+", app_slug)) and user_id.isdigit(), "invalid bot identity")
            run("git", "config", "user.name", f"{app_slug}[bot]")
            run("git", "config", "user.email", f"{user_id}+{app_slug}[bot]@users.noreply.github.com")
            run("git", "add", "--", "schemas/")
            run("git", "-c", "commit.gpgsign=false", "commit", "-m", message)
            run("git", "push", "origin", f"HEAD:refs/heads/{branch}")
    body = (f"<!-- schema-issue:{number} -->\nCloses #{number}\n\n"
            "Generated contract validated offline. Review the fields, version identity and compatibility policy. "
            "Merge creates an immutable schema tag; Registry registration remains a separate explicit operation.\n")
    with tempfile.TemporaryDirectory(prefix="schema-pr-") as tmp:
        path = Path(tmp) / "body.md"
        path.write_text(body)
        url = run("gh", "pr", "create", "--repo", repo, "--base", "main", "--head", branch,
                  "--title", f"schema: contract from Issue #{number}", "--body-file", str(path), "--label", "schema")
    print(url)
    return url


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--issue", required=True, type=int)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args()
    try:
        process(args.repo, args.issue, os.environ.get("APP_SLUG", ""), os.environ.get("APP_USER_ID", ""))
    except Exception as error:
        parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
