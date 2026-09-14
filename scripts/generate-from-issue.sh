#!/usr/bin/env bash
# Compatible CLI; use '-' to read the Issue body from stdin.
set -euo pipefail
exec python3 "$(dirname "$0")/contracts.py" generate "$@"
