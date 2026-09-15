#!/usr/bin/env bash
set -euo pipefail

# Register a schema version in Apicurio Registry
# Usage: ./register.sh --registry http://localhost:8085 --group default --schema schemas/avro/fast/ride/requested/v1

REGISTRY=""
GROUP="default"
SCHEMA_DIR=""
TOKEN="${APICURIO_TOKEN:-}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --registry) REGISTRY="$2"; shift 2 ;;
    --group)    GROUP="$2"; shift 2 ;;
    --schema)   SCHEMA_DIR="$2"; shift 2 ;;
    --token)    TOKEN="$2"; shift 2 ;;
    *) echo "Unknown: $1"; exit 1 ;;
  esac
done

AUTH=()
[ -n "$TOKEN" ] && AUTH=(-H "Authorization: Bearer $TOKEN")

if [ -z "$REGISTRY" ] || [ -z "$SCHEMA_DIR" ]; then
  echo "Usage: $0 --registry <url> --group <group> --schema <dir>"
  exit 1
fi

# Read metadata
META="$SCHEMA_DIR/.meta.json"
if [ ! -f "$META" ]; then
  echo "ERROR: metadata not found: $META"
  exit 1
fi

NAME=$(python3 -c "import json; print(json.load(open('$META'))['name'])")
TYPE=$(python3 -c "import json; print(json.load(open('$META'))['type'])")
if [ "$TYPE" != "avro" ]; then
  echo "ERROR: only Avro schemas are supported"
  exit 1
fi
COMPAT=$(python3 -c "import json; print(json.load(open('$META'))['compatibility'])")

# Find schema file
FILE="$SCHEMA_DIR/schema.avsc"
CONTENT_TYPE="application/vnd.apache.avro+json"

if [ ! -f "$FILE" ]; then
  echo "ERROR: schema file not found: $FILE"
  exit 1
fi

echo "Registering $NAME ($TYPE) v$(python3 -c "import json; print(json.load(open('$META'))['version'])")..."

# Build the artifact payload
ARTIFACT_ID="$NAME"
SCHEMA_CONTENT=$(cat "$FILE" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")

# Register in Apicurio
RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$REGISTRY/apis/registry/v2/groups/$GROUP/artifacts" \
  -H "Content-Type: $CONTENT_TYPE" \
  -H "X-Registry-ArtifactId: $ARTIFACT_ID" \
  -H "X-Registry-Name: $NAME" \
  "${AUTH[@]}" \
  --data-binary "@$FILE")

HTTP_CODE=$(echo "$RESPONSE" | tail -1)
BODY=$(echo "$RESPONSE" | head -n -1)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "201" ]; then
  GLOBAL_ID=$(echo "$BODY" | python3 -c "import sys,json; print(json.load(sys.stdin).get('globalId', 'unknown'))")
  echo "  ✅ Registered: globalId=$GLOBAL_ID"
else
  echo "  ❌ Failed (HTTP $HTTP_CODE): $BODY"
  exit 1
fi

# Set compatibility
curl -s -X PUT "$REGISTRY/apis/registry/v2/groups/$GROUP/artifacts/$ARTIFACT_ID/rules" \
  -H "Content-Type: application/json" \
  "${AUTH[@]}" \
  -d "{\"config\": \"$COMPAT\", \"ruleType\": \"COMPATIBILITY\"}" > /dev/null

echo "  ✅ Compatibility set: $COMPAT"
