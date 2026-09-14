#!/usr/bin/env python3
"""Create one GitHub commit from the current working tree using the Git Database API."""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def api(token: str, path: str, method: str = "GET", payload: dict | None = None) -> dict:
    """Call one validated GitHub API path over a fixed GitHub HTTPS host."""
    if not re.fullmatch(r"repos/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.:/-]+)?", path):
        raise ValueError("invalid GitHub API path")
    body = None if payload is None else json.dumps(payload)
    connection = http.client.HTTPSConnection("api.github.com", timeout=30)
    try:
        connection.request(
            method,
            f"/{path}",
            body=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "hellnet-actions",
            },
        )
        response = connection.getresponse()
        raw = response.read().decode("utf-8")
        if response.status >= 400:
            raise RuntimeError(f"GitHub API {method} {path} failed ({response.status}): {raw}")
        return json.loads(raw)
    finally:
        connection.close()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def changed_files(root: str) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--", root], text=True
    )
    for line in status.splitlines():
        if not line:
            continue
        code = line[:2]
        path = line[3:]
        if "->" in path:
            path = path.split(" -> ", 1)[1]
        if code == "??":
            entries.append(("added", path))
        elif "D" in code:
            entries.append(("deleted", path))
        else:
            entries.append(("modified", path))
    return entries


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--root", default="schemas")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is required")

    changes = changed_files(args.root)
    if not changes:
        raise SystemExit("no changed files")

    base_sha = api(token, f"repos/{args.repo}/git/ref/heads/{args.base}")["object"]["sha"]
    base_tree = api(token, f"repos/{args.repo}/git/commits/{base_sha}")["tree"]["sha"]
    tree_entries = []
    for change, path in changes:
        if change == "deleted":
            tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        content = base64.b64encode(Path(path).read_bytes()).decode("ascii")
        blob = api(token, f"repos/{args.repo}/git/blobs", "POST", {"content": content, "encoding": "base64"})
        tree_entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

    tree = api(
        token,
        f"repos/{args.repo}/git/trees",
        "POST",
        {"base_tree": base_tree, "tree": tree_entries},
    )
    commit = api(
        token,
        f"repos/{args.repo}/git/commits",
        "POST",
        {"message": args.message, "tree": tree["sha"], "parents": [base_sha]},
    )
    commit_sha = commit["sha"]
    api(
        token,
        f"repos/{args.repo}/git/refs",
        "POST",
        {"ref": f"refs/heads/{args.branch}", "sha": commit_sha},
    )
    verified = api(token, f"repos/{args.repo}/git/commits/{commit_sha}")["verification"]["verified"]
    print(json.dumps({"branch": args.branch, "commit": commit_sha, "verification_verified": verified}))
    if not verified:
        raise SystemExit("GitHub did not verify the GitHub App commit")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # keep token and request body out of logs
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
