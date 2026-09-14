#!/usr/bin/env bash
# Apicurio v2 test only: never modifies artifact content or compatibility rules.
set -euo pipefail
exec python3 "$(dirname "$0")/registry_check.py" "$@"
