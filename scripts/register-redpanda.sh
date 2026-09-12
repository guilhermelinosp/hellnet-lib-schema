#!/usr/bin/env bash
# Discover and register Avro contracts using topic names as subjects.
set -euo pipefail

SCRIPT_DIR=$(CDPATH=; cd -- "$(dirname -- "$0")" && pwd)
REPO_ROOT=$(CDPATH=; cd -- "$SCRIPT_DIR/.." && pwd)
SCHEMAS_DIR="${SCHEMAS_DIR:-$REPO_ROOT/schemas/avro}"
REGISTRY_URL="${REDPANDA_SCHEMA_REGISTRY_URL:-http://localhost:8081}"
BROKERS="${REDPANDA_BROKERS:-localhost:9092}"
MODE=dry-run
CREATE_TOPICS=false

usage() {
  cat <<'EOF'
Usage: register-redpanda.sh [--dry-run | --apply] [--registry URL] [--create-topics]

Dry-run is the default. --create-topics requires --apply and rpk on PATH.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry-run ;;
    --apply) MODE=apply ;;
    --registry)
      [ "$#" -ge 2 ] || { echo "Missing value for --registry" >&2; exit 2; }
      REGISTRY_URL=$2
      shift
      ;;
    --create-topics) CREATE_TOPICS=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$CREATE_TOPICS" = true ] && [ "$MODE" != apply ]; then
  echo "--create-topics requires --apply; refusing to create anything." >&2
  exit 2
fi

command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; exit 1; }
if [ "$MODE" = apply ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required for --apply" >&2; exit 1; }
fi

mapfile -t schema_files < <(LC_ALL=C printf '%s\n' "$SCHEMAS_DIR"/*/v*/schema.avsc | LC_ALL=C sort)
if [ "${#schema_files[@]}" -eq 0 ] || [ ! -f "${schema_files[0]}" ]; then
  echo "No Avro schemas found under $SCHEMAS_DIR" >&2
  exit 1
fi

for schema_file in "${schema_files[@]}"; do
  [ -f "$schema_file" ] || continue
  schema_dir=$(dirname -- "$(dirname -- "$schema_file")")
  directory_name=$(basename -- "$schema_dir")
  version_dir=$(basename -- "$(dirname -- "$schema_file")")
  if [[ ! "$version_dir" =~ ^v([1-9][0-9]*)$ ]]; then
    echo "Invalid schema version directory: $schema_file" >&2
    exit 1
  fi
  version=${BASH_REMATCH[1]}

  metadata=$(python3 - "$schema_file" "$schema_dir" "$directory_name" "$version" <<'PY'
import json
import pathlib
import re
import sys

path, schema_dir, directory_name, version = sys.argv[1:]
try:
    document = json.loads(pathlib.Path(path).read_text())
    meta = json.loads((pathlib.Path(schema_dir) / ".meta.json").read_text())
except (OSError, json.JSONDecodeError) as exc:
    raise SystemExit(f"Invalid schema metadata in {path}: {exc}")

if not isinstance(document, dict) or document.get("type") != "record":
    raise SystemExit(f"{path} is not an Avro record")
namespace = document.get("namespace")
record_name = document.get("name")
if not isinstance(namespace, str) or not namespace:
    raise SystemExit(f"{path} has no namespace")
if not isinstance(record_name, str) or not record_name:
    raise SystemExit(f"{path} has no record name")

configured_name = meta.get("name")
if isinstance(configured_name, str) and configured_name.startswith("ride."):
    subject = configured_name
    topic = configured_name
elif re.fullmatch(r"fast-[a-z0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*", directory_name):
    domain, event = directory_name.removeprefix("fast-").split("-", 1)
    topic = f"{domain}.{event.replace('-', '.')}.v{version}"
    subject = topic
else:
    subject = directory_name
    topic = f"{directory_name.replace('-', '.')}.v{version}"
print("\t".join((namespace, record_name, subject, topic)))
PY
  )

  IFS=$'\t' read -r namespace record_name subject topic <<<"$metadata"
  endpoint="${REGISTRY_URL%/}/subjects/${subject}/versions"
  printf 'schema=%s namespace=%s record=%s version=v%s\n' \
    "$schema_file" "$namespace" "$record_name" "$version"
  printf '  subject=%s\n  endpoint=%s\n  topic=%s\n' "$subject" "$endpoint" "$topic"

  if [ "$MODE" = dry-run ]; then
    continue
  fi

  if [ "$CREATE_TOPICS" = true ]; then
    if command -v rpk >/dev/null 2>&1; then
      rpk topic create "$topic" --brokers "$BROKERS" --if-not-exists >/dev/null
    else
      echo "warning: rpk not installed; topic not created: $topic" >&2
    fi
  fi

  python3 -c 'import json, pathlib, sys; print(json.dumps({"schema": json.dumps(json.loads(pathlib.Path(sys.argv[1]).read_text()), separators=(",", ":"))}))' "$schema_file" \
    | curl --fail-with-body --silent --show-error \
    --connect-timeout 5 --max-time 30 \
    --header 'Content-Type: application/vnd.schemaregistry.v1+json' \
    --data-binary @- "$endpoint" >/dev/null
  echo "  registered"
done
