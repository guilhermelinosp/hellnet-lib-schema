#!/usr/bin/env bash
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
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) MODE=dry-run ;;
    --apply) MODE=apply ;;
    --registry) [ "$#" -ge 2 ] || { echo "Missing registry URL" >&2; exit 2; }; REGISTRY_URL=$2; shift ;;
    --create-topics) CREATE_TOPICS=true ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$CREATE_TOPICS" = true ] && [ "$MODE" != apply ]; then
  echo "--create-topics requires --apply" >&2
  exit 2
fi
command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }
if [ "$MODE" = apply ]; then command -v curl >/dev/null || { echo "curl is required" >&2; exit 1; }; fi

mapfile -t schema_files < <(find "$SCHEMAS_DIR" -type f -path '*/v*/schema.avsc' | LC_ALL=C sort)
[ "${#schema_files[@]}" -gt 0 ] || { echo "No Avro schemas found under $SCHEMAS_DIR" >&2; exit 1; }

for schema_file in "${schema_files[@]}"; do
  schema_dir=$(dirname -- "$schema_file")
  meta="$schema_dir/.meta.json"
  [ -f "$meta" ] || { echo "Missing metadata: $meta" >&2; exit 1; }
  schema_name=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["name"])' "$meta")
  topic="$schema_name"
  subject="$schema_name"
  endpoint="${REGISTRY_URL%/}/subjects/${subject}/versions"
  printf 'schema=%s subject=%s topic=%s\n' "$schema_file" "$subject" "$topic"
  if [ "$MODE" = dry-run ]; then continue; fi
  if [ "$CREATE_TOPICS" = true ] && command -v rpk >/dev/null 2>&1; then
    rpk topic create "$topic" --brokers "$BROKERS" --if-not-exists >/dev/null
  fi
  python3 -c 'import json,sys; print(json.dumps({"schema": json.dumps(json.load(open(sys.argv[1])), separators=(",", ":"))}))' "$schema_file" \
    | curl --fail-with-body --silent --show-error --connect-timeout 5 --max-time 30 \
      -H 'Content-Type: application/vnd.schemaregistry.v1+json' --data-binary @- "$endpoint" >/dev/null
  echo "registered"
done
