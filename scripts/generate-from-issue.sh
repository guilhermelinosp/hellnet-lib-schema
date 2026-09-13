#!/usr/bin/env bash
set -euo pipefail

# === Helpers ===

# Extract the non-empty content of an Issue Form section. Issue Forms render
# an empty line between a heading and its value, and may add more empty lines
# inside a multiline field, so parsing must be heading-delimited rather than
# based on the line immediately following the heading.
extract_section() {
  local body="$1" section="$2"
  printf '%s\n' "$body" | awk -v section="$section" '
    /^### / {
      in_section = ($0 == "### " section)
      next
    }
    in_section && NF { print }
  '
}

extract_value() {
  extract_section "$1" "$2" | awk 'NF { print; exit }' | xargs
}

# Parse issue body fields from GitHub issue template
parse_issue_body() {
  local body="$1"
  NAME=$(extract_value "$body" "Schema name")
  TYPE=$(extract_value "$body" "Format")
  COMPAT=$(extract_value "$body" "Compatibility level")
  FIELDS=$(extract_section "$body" "Fields (YAML)" |
    sed '/^[[:space:]]*```[[:alnum:]_-]*[[:space:]]*$/d;/^[[:space:]]*$/d')

  NAME="${NAME:-unknown}"
  TYPE="${TYPE:-avro}"
  COMPAT="${COMPAT:-BACKWARD}"
  # GitHub issue forms rejects "NONE" keyword, use "NO_CHECK" instead
  if [ "$COMPAT" = "NO_CHECK" ]; then
    COMPAT="NONE"
  fi
}

# Parse YAML field blocks into pipe-separated lines: name|type|default|required
parse_fields() {
  echo "$1" | awk '
    BEGIN { f=""; t=""; d=""; r=""; sep="|" }
    /^- / {
      if (f != "") print f sep t sep d sep r
      f=substr($0, index($0,": ")+2)
      t=""; d=""; r=""
    }
    /^  type:/     { t=substr($0, index($0,": ")+2) }
    /^  default:/  { d=substr($0, index($0,": ")+2) }
    /^  required:/ { r=substr($0, index($0,": ")+2) }
    END { if (f != "") print f sep t sep d sep r }
  '
}

# Validate JSON file
validate_json_file() {
  python3 -c "import json; json.load(open('$1'))" 2>/dev/null || {
    echo "ERROR: Invalid JSON in $1"
    exit 1
  }
}

# === Generator: Avro ===

generate_avro() {
  local name="$1" fields="$2" output="$3" version="$4"
  local fast_name domain event avro_name namespace doc

  # Avro schemas are Fast-only: the name is part of the public contract and
  # follows the fast-{domain}-{event} convention. JSON and Protobuf may use
  # the generic hellnet-{domain}-{event} convention instead.
  if [[ ! "$name" =~ ^fast-[a-z0-9]+-[a-z0-9]+([a-z0-9-]*[a-z0-9])?$ ]]; then
    echo "ERROR: Avro schemas must use the fast-{domain}-{event} convention (got: $name). Use fast-* for Avro, or switch to JSON/Protobuf for hellnet-* names." >&2
    exit 1
  fi
  fast_name="${name#fast-}"
  domain="${fast_name%%-*}"
  event="${fast_name#*-}"
  if [ -z "$domain" ] || [ -z "$event" ] || [[ "$event" == *--* ]]; then
    echo "ERROR: invalid Fast Avro schema name (domain/event must be non-empty): $name" >&2
    exit 1
  fi

  avro_name=$(printf '%s\n' "${domain}-${event}" | awk -F- '{for (i = 1; i <= NF; i++) $i = toupper(substr($i, 1, 1)) substr($i, 2)}1' | tr -d ' ')
  avro_name="Fast${avro_name}V${version}"
  namespace="fast.events.${domain}.v${version}"
  case "${domain}-${event}" in
    ride-requested) doc="Published when a ride is requested in Fast." ;;
    ride-accepted) doc="Published when a driver accepts a ride in Fast." ;;
    ride-completed) doc="Published when a ride is completed in Fast." ;;
    ride-cancelled|ride-canceled) doc="Published when a ride is cancelled in Fast." ;;
    payment-requested) doc="Published when payment is requested for a ride in Fast." ;;
    payment-authorized) doc="Published when payment is authorized for a ride in Fast." ;;
    payment-captured) doc="Published when payment is captured for a ride in Fast." ;;
    payment-failed) doc="Published when payment fails for a ride in Fast." ;;
    payment-refunded) doc="Published when payment is refunded for a ride in Fast." ;;
    driver-registered) doc="Published when a driver is registered in Fast." ;;
    driver-available) doc="Published when a driver becomes available in Fast." ;;
    driver-unavailable) doc="Published when a driver becomes unavailable in Fast." ;;
    driver-location-updated) doc="Published when a driver's location is updated in Fast." ;;
    *)
      doc="Published when ${event//-/ } occurs in ${domain} in Fast."
      ;;
  esac

  exec 3>"$output"
  echo '{' >&3
  echo "  \"namespace\": \"$namespace\"," >&3
  echo '  "type": "record",' >&3
  echo "  \"name\": \"$avro_name\"," >&3
  echo "  \"doc\": \"$doc\"," >&3
  echo '  "fields": [' >&3

  local first=true
  while IFS='|' read -r fname ftype fdefault frequired; do
    [ -z "$fname" ] || [ -z "$ftype" ] && continue

    [ "$first" = false ] && echo "," >&3
    first=false

    echo -n '    { "name": "'"$fname"'", "type": ' >&3
    if [ "$frequired" = "false" ]; then
      echo -n '["null", "'"$ftype"'"]' >&3
      if [ -n "$fdefault" ]; then
        echo -n ', "default": '"$fdefault" >&3
      else
        echo -n ', "default": null' >&3
      fi
    else
      echo -n '"'"$ftype"'"' >&3
      [ -n "$fdefault" ] && echo -n ', "default": '"$fdefault" >&3
    fi
    echo -n ' }' >&3
  done < <(parse_fields "$fields")

  echo '' >&3
  echo '  ]' >&3
  echo '}' >&3
  exec 3>&-

  validate_json_file "$output"
}

# === Generator: JSON Schema ===

generate_json() {
  local name="$1" fields="$2" output="$3"
  local title
  title=$(echo "$name" | tr '-' ' ' | awk '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1')

  exec 3>"$output"
  echo '{' >&3
  echo '  "$schema": "http://json-schema.org/draft-07/schema#",' >&3
  echo "  \"title\": \"$title\"," >&3
  echo '  "type": "object",' >&3
  echo '  "properties": {' >&3

  local first=true
  while IFS='|' read -r fname ftype fdefault frequired; do
    [ -z "$fname" ] && continue
    [ "$first" = false ] && echo "," >&3
    first=false
    echo -n "    \"$fname\": { \"type\": \"$ftype\" }" >&3
  done < <(parse_fields "$fields")

  echo '' >&3
  echo '  },' >&3
  echo '  "required": [' >&3

  local first=true
  while IFS='|' read -r fname ftype fdefault frequired; do
    [ -z "$fname" ] && continue
    if [ "$frequired" != "false" ]; then
      [ "$first" = false ] && echo "," >&3
      first=false
      echo -n "    \"$fname\"" >&3
    fi
  done < <(parse_fields "$fields")

  echo '' >&3
  echo '  ]' >&3
  echo '}' >&3
  exec 3>&-

  validate_json_file "$output"
}

# === Generator: Protobuf ===

generate_protobuf() {
  local name="$1" fields="$2" output="$3"
  local msg_name
  msg_name=$(echo "$name" | awk -F'-' '{for(i=1;i<=NF;i++) $i=toupper(substr($i,1,1)) substr($i,2)}1' | tr -d ' ')

  exec 3>"$output"
  echo 'syntax = "proto3";' >&3
  echo 'package hellnet.events.v1;' >&3
  echo '' >&3
  echo 'option csharp_namespace = "Hellnet.Events.V1";' >&3
  echo '' >&3
  echo "message $msg_name {" >&3

  local idx=0
  while IFS='|' read -r fname ftype fdefault frequired; do
    [ -z "$fname" ] && continue
    idx=$((idx + 1))
    echo "  $ftype $fname = $idx;" >&3
  done < <(parse_fields "$fields")

  echo '}' >&3
  exec 3>&-

  if ! grep -q 'syntax = "proto3"' "$output"; then
    echo "ERROR: Invalid protobuf schema generated"
    exit 1
  fi
}

# === Main ===

parse_issue_body "${1:-}"

# === Security: strictly validate untrusted inputs from the issue body ===
# NAME/TYPE come from the issue body and are later used in shell commands
# (branch name, tag, commit message, PR title). Restrict to a safe allowlist
# to prevent command injection in the workflow steps that consume them.
if ! echo "$NAME" | grep -qE '^[A-Za-z0-9_-]+$'; then
  echo "ERROR: invalid schema name (allowed: [A-Za-z0-9_-]): $NAME"
  exit 1
fi
case "$TYPE" in
  avro|json|protobuf) ;;
  *) echo "ERROR: invalid schema type (allowed: avro|json|protobuf): $TYPE"; exit 1 ;;
esac
case "$COMPAT" in
  BACKWARD|FORWARD|FULL|NONE) ;;
  *) echo "ERROR: invalid compatibility level (allowed: BACKWARD|FORWARD|FULL|NONE): $COMPAT"; exit 1 ;;
esac

VERSION=1

# Fast Avro canonical layout:
# schemas/avro/fast/{domain}/{event}/v{version}
if [ "$TYPE" = "avro" ] && [[ "$NAME" == fast-* ]]; then
  FAST_NAME="${NAME#fast-}"
  DOMAIN="${FAST_NAME%%-*}"
  EVENT="${FAST_NAME#*-}"

  if [ -z "$DOMAIN" ] || [ -z "$EVENT" ] || [ "$FAST_NAME" = "$DOMAIN" ]; then
    echo "ERROR: invalid Fast Avro schema name (expected fast-{domain}-{event}): $NAME" >&2
    exit 1
  fi

  SCHEMA_DIR="schemas/avro/fast/${DOMAIN}/${EVENT}"
else
  SCHEMA_DIR="schemas/${TYPE}/${NAME}"
fi

# Auto-increment version inside the canonical schema directory
if [ -d "$SCHEMA_DIR" ]; then
  last=$(ls -1 "$SCHEMA_DIR" 2>/dev/null | grep -E '^v[0-9]+$' | sort -t'v' -k2 -n | tail -1)
  if [ -n "$last" ]; then
    VERSION=$(( ${last#v} + 1 ))
  fi
fi

mkdir -p "$SCHEMA_DIR/v${VERSION}"

case "$TYPE" in
  avro)
    generate_avro "$NAME" "$FIELDS" "$SCHEMA_DIR/v${VERSION}/schema.avsc" "$VERSION"
    ;;
  json)
    generate_json "$NAME" "$FIELDS" "$SCHEMA_DIR/v${VERSION}/schema.json"
    ;;
  protobuf)
    generate_protobuf "$NAME" "$FIELDS" "$SCHEMA_DIR/v${VERSION}/schema.proto"
    ;;
  *)
    echo "ERROR: unknown type: $TYPE"
    exit 1
    ;;
esac

# Write metadata.
# Fast Avro metadata name is also the stable topic/subject name.
META_NAME="$NAME"
if [ "$TYPE" = "avro" ] && [[ "$NAME" == fast-* ]]; then
  EVENT_TOPIC="${EVENT//-/.}"
  META_NAME="fast.${DOMAIN}.${EVENT_TOPIC}.v${VERSION}"
fi

cat > "$SCHEMA_DIR/v${VERSION}/.meta.json" << META
{
  "name": "$META_NAME",
  "type": "$TYPE",
  "version": $VERSION,
  "compatibility": "$COMPAT",
  "createdAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
META

echo "NAME=$NAME"
echo "TYPE=$TYPE"
echo "VERSION=$VERSION"
EXT="avsc"
[ "$TYPE" = "json" ] && EXT="json"
[ "$TYPE" = "protobuf" ] && EXT="proto"
echo "PATH=${SCHEMA_DIR}/v${VERSION}/schema.${EXT}"
