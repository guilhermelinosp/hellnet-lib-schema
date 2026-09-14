"""Calculate the next repository release without changing Git state."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path


SEMVER = re.compile(r"^v([0-9]+)\.([0-9]+)\.([0-9]+)(?:-beta\.([0-9]+))?$")


def next_version(tags, subjects):
    versions = []
    for tag in tags:
        match = SEMVER.match(tag)
        if match:
            major, minor, patch, beta = match.groups()
            versions.append(((int(major), int(minor), int(patch), beta is not None, int(beta or 0)), tag))
    if not versions:
        return "v1.0.0"
    (major, minor, patch, is_beta, beta), _ = max(versions)
    if is_beta:
        return f"v{major}.{minor}.{patch}-beta.{beta + 1}"
    if any(subject.startswith("feat") for subject in subjects):
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"v{major}.{minor}.{patch}"


def calculate(repo=Path(".")):
    repo = Path(repo)
    tags = subprocess.check_output(["git", "tag", "--list", "v*"], cwd=repo, text=True).splitlines()
    subjects = subprocess.check_output(["git", "log", "--format=%s", "-100"], cwd=repo, text=True).splitlines()
    return next_version(tags, subjects)


if __name__ == "__main__":
    print(calculate())
