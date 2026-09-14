#!/usr/bin/env bash
# Validate contracts, metadata and canonical layout offline.
set -euo pipefail
exec python3 "$(dirname "$0")/contracts.py" validate "$@"
