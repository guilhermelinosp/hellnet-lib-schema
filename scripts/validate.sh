#!/usr/bin/env bash
# Validate schema syntax, metadata, and canonical repository layout.
# Usage: ./scripts/validate.sh [schemas/root/dir]
set -euo pipefail

SCHEMAS_DIR="${1:-schemas}"
HAS_ERROR=false
SCHEMA_COUNT=0

fail() {
  echo "  FAIL: $*"
  HAS_ERROR=true
}

validate_avro() {
  local file="$1"
  if ! python3 - "$file" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path) as fh:
    schema = json.load(fh)

if schema.get("type") != "record":
    raise SystemExit(f"{path}: Avro top-level type must be record")
if not isinstance(schema.get("fields"), list):
    raise SystemExit(f"{path}: Avro fields must be an array")

print(f"  OK schema: {schema.get('name', 'unknown')} ({len(schema['fields'])} fields)")
PY
  then
    HAS_ERROR=true
  fi
}

validate_json() {
  local file="$1"
  if ! python3 - "$file" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path) as fh:
    schema = json.load(fh)

if not schema.get("$schema", "").startswith("http"):
    raise SystemExit(f"{path}: missing/invalid $schema")
if "properties" not in schema:
    raise SystemExit(f"{path}: missing properties")

print(f"  OK schema: {schema.get('title', 'unknown')} ({len(schema['properties'])} properties)")
PY
  then
    HAS_ERROR=true
  fi
}

validate_proto() {
  local file="$1"
  if ! grep -q 'syntax = "proto3"' "$file"; then
    fail "$file: missing proto3 syntax"
    return
  fi
  echo "  OK schema: $(basename "$file")"
}

validate_meta() {
  local file="$1"
  if ! python3 - "$file" <<'PY'
import json
import os
import sys

path = sys.argv[1]
with open(path) as fh:
    meta = json.load(fh)

for field in ("name", "type", "version", "compatibility"):
    if field not in meta:
        raise SystemExit(f"{path}: missing metadata field {field}")

if meta["type"] not in ("avro", "json", "protobuf"):
    raise SystemExit(f"{path}: invalid metadata type {meta['type']}")

if meta["compatibility"] not in ("BACKWARD", "FORWARD", "FULL", "NONE"):
    raise SystemExit(f"{path}: invalid compatibility {meta['compatibility']}")

version_dir = os.path.basename(os.path.dirname(path))
if version_dir != f"v{meta['version']}":
    raise SystemExit(
        f"{path}: metadata version {meta['version']} does not match directory {version_dir}"
    )

print(f"  OK metadata: {meta['name']} v{meta['version']} ({meta['type']})")
PY
  then
    HAS_ERROR=true
  fi
}

validate_fast_avro_contract() {
  local meta="$1"

  if ! python3 - "$SCHEMAS_DIR" "$meta" <<'PY'
import json
import re
import sys
from pathlib import Path

schemas_root = Path(sys.argv[1])
meta_path = Path(sys.argv[2])
schema_path = meta_path.parent / "schema.avsc"

try:
    relative = meta_path.relative_to(schemas_root)
except ValueError:
    raise SystemExit(f"{meta_path}: not under {schemas_root}")

# Only Fast Avro metadata lives under avro/fast/{domain}/{event}/v{version}/
# (6 path parts). Everything else is validated by schema-type rules only.
parts = relative.parts
if len(parts) != 6 or parts[0] != "avro" or parts[1] != "fast":
    raise SystemExit(0)

if not schema_path.is_file():
    raise SystemExit(f"{meta_path}: missing sibling schema.avsc")

domain, event, version_dir, filename = parts[2], parts[3], parts[4], parts[5]
if filename != ".meta.json":
    raise SystemExit(f"{meta_path}: invalid metadata filename")
if not re.fullmatch(r"[a-z0-9]+", domain):
    raise SystemExit(f"{meta_path}: invalid Fast domain {domain!r}")
if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", event):
    raise SystemExit(f"{meta_path}: invalid Fast event {event!r}")
if not re.fullmatch(r"v[1-9][0-9]*", version_dir):
    raise SystemExit(f"{meta_path}: invalid version directory {version_dir!r}")

version = int(version_dir[1:])
with open(meta_path) as fh:
    meta = json.load(fh)
with open(schema_path) as fh:
    schema = json.load(fh)

expected_meta_name = f"fast.{domain}.{event.replace('-', '.')}.v{version}"
expected_namespace = f"fast.events.{domain}.v{version}"

words = [domain, *event.split("-")]
pascal = "".join(word[:1].upper() + word[1:] for word in words)
expected_record = f"Fast{pascal}V{version}"

checks = [
    (meta.get("type") == "avro", f"metadata type must be avro"),
    (meta.get("version") == version, f"metadata version must be {version}"),
    (meta.get("name") == expected_meta_name,
     f"metadata name must be {expected_meta_name!r}, got {meta.get('name')!r}"),
    (schema.get("namespace") == expected_namespace,
     f"namespace must be {expected_namespace!r}, got {schema.get('namespace')!r}"),
    (schema.get("name") == expected_record,
     f"record name must be {expected_record!r}, got {schema.get('name')!r}"),
]

errors = [message for ok, message in checks if not ok]
if errors:
    raise SystemExit(f"{meta_path}: " + "; ".join(errors))

print(
    f"  OK Fast contract: {domain}/{event}/{version_dir} "
    f"-> {expected_meta_name} -> {expected_record}"
)
PY
  then
    HAS_ERROR=true
  fi
}

echo "=== Validating Avro ==="
while IFS= read -r file; do
  [[ -n "$file" ]] || continue
  SCHEMA_COUNT=$((SCHEMA_COUNT + 1))
  echo "Validating: $file"
  validate_avro "$file"
done < <(find "$SCHEMAS_DIR/avro" -type f -name 'schema.avsc' 2>/dev/null | LC_ALL=C sort)

echo "=== Validating JSON ==="
while IFS= read -r file; do
  [[ -n "$file" ]] || continue
  SCHEMA_COUNT=$((SCHEMA_COUNT + 1))
  echo "Validating: $file"
  validate_json "$file"
done < <(find "$SCHEMAS_DIR/json" -type f -name 'schema.json' 2>/dev/null | LC_ALL=C sort)

echo "=== Validating Protobuf ==="
while IFS= read -r file; do
  [[ -n "$file" ]] || continue
  SCHEMA_COUNT=$((SCHEMA_COUNT + 1))
  echo "Validating: $file"
  validate_proto "$file"
done < <(find "$SCHEMAS_DIR/protobuf" -type f -name 'schema.proto' 2>/dev/null | LC_ALL=C sort)

echo "=== Validating metadata and layout ==="
while IFS= read -r file; do
  [[ -n "$file" ]] || continue
  echo "Validating: $file"
  validate_meta "$file"
  validate_fast_avro_contract "$file"
done < <(find "$SCHEMAS_DIR" -type f -name '.meta.json' 2>/dev/null | LC_ALL=C sort)

if [[ "$SCHEMA_COUNT" -eq 0 ]]; then
  echo "FAIL: no schemas found under $SCHEMAS_DIR"
  exit 1
fi

if [[ "$HAS_ERROR" == true ]]; then
  echo "Validation failed"
  exit 1
fi

echo "All schemas valid"
