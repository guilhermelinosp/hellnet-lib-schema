# Contributing contracts

This repository is the source of truth for event contracts, not a Go library or an
application. Keep historical schema files byte-for-byte unchanged. Publishing a
contract, deploying it to a Registry and producing events are separate operations.

## Development checks

Use Python 3.11+ and a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip --isolated install -r requirements.txt
python -m unittest discover -s tests -v
bash scripts/validate.sh schemas
git fetch origin main
python scripts/evolution.py --base origin/main
```

CI installs the same pinned dependencies, including the Protobuf compiler, and runs
the same suite. No credentials, running Registry, Kafka broker, Go module, containers
or generated application code are needed. Tests use temporary repositories and
mock GitHub/Registry responses; they never create real Issues, PRs or artifacts.

Run commands from the checkout root. CLI schema roots must resolve inside the
current directory; Registry checks accept only paths inside its `schemas/` tree.
Symlinks cannot escape these boundaries. `--base` accepts only a full commit SHA,
`main` or `origin/main`, never arbitrary revision expressions or Git options.

## Issue fields

The New Schema form accepts a YAML list of field mappings. The parser reads
heading-delimited sections, safely parses YAML and serializes JSON correctly.
Duplicate keys/names, aliases, unknown options, unsafe paths and malformed schemas
are errors. Generation validates in a temporary directory before publishing the
complete version directory. The shell entry point still emits `NAME`, `TYPE`,
`VERSION` and `PATH` as `KEY=value` lines.

| Option | Meaning |
|--------|---------|
| `name` | Unique Avro/Protobuf-compatible identifier |
| `type` | A type in the selected format; never implicitly converted |
| `required` | Boolean, defaults to true; see format-specific behavior below |
| `default` | A YAML value, not an already JSON-encoded string; Avro/JSON only |
| `doc` | Description of a field |
| `number` | Mandatory stable field number for Protobuf only |

- **Avro:** the generator preserves the Fast-only `fast-{domain}-{event}` naming
  policy. Fields accept primitive or structured Avro types. Optional fields generate a nullable
  union with a default. Defaults must match the first union branch. Existing
  logical types, names and defaults are validated, not merely JSON syntax.
- **JSON:** use JSON Schema types (`number`, `integer`, `boolean`, etc.). Optional
  fields are omitted from `required`. A default is an annotation, not automatic
  payload insertion. The form handles type declarations; author advanced
  constraints directly in the generated PR before merge.
- **Protobuf:** use proto3 scalar types (`double`, `int64`, `bool`, etc.) and supply
  an explicit `number` for every field. Numbers must be unique, positive, within
  range and outside 19000–19999. `required: false` generates `optional` presence;
  `required: true` emits a normal proto3 scalar, not a wire-level required field.
  Explicit defaults are rejected. Never renumber fields when changing order.

Example Protobuf fields:

```yaml
- name: id
  type: string
  number: 1
- name: quantity
  type: int32
  number: 2
```

Example Avro fields:

```yaml
- name: id
  type: string
  doc: Stable event identifier
- name: note
  type: string
  required: false
  default: null
```

## Evolution policy

1. The first directory is `v1`; subsequent directories are sequential without
   gaps. A version already on the PR base cannot be modified, renamed, deleted or
   extended with extra files. CI compares actual contents against the PR base SHA.
2. Fast Avro preserves `fast/{domain}/{event}/vN`, metadata/topic/subject
   `fast.{domain}.{event-as-dots}.vN`, namespace `fast.events.{domain}.vN` and
   record `Fast{Domain}{Event}VN`. A new N is a separate identity. CI prints
   `NEW IDENTITY`, not a misleading cross-version compatibility success. Producers
   and consumers must explicitly migrate to the new topic/contract.
3. Generic Avro, JSON and Protobuf keep their metadata name across directory
   versions. The **new version's** metadata selects BACKWARD, FORWARD, FULL or
   NONE (`NO_CHECK` from the form maps to NONE). Checks compare adjacent versions,
   not all history; no transitive guarantee is claimed.
4. Generic Avro uses Apache Avro reader/writer compatibility checks and also rejects
   logical-type changes. JSON uses a conservative inclusion check for types, enum,
   object properties/required/additionalProperties and homogeneous array items.
   Changed advanced constraints cannot pass without a dedicated checker or an
   explicitly reviewed NONE policy. An optional property may still be incompatible
   when the old schema allowed arbitrary values for that property.
5. Protobuf uses compiled descriptors and a stricter, additive-only policy for all
   non-NONE modes: existing messages, field numbers/names/types, options, presence,
   oneofs, nested types and enums must remain unchanged. Independent scalar fields
   and top-level messages may be added. Some wire-compatible changes are
   deliberately rejected; this is not a complete binary/JSON compatibility oracle.
6. NONE is a visible opt-out, not proof of safety. Review the migration and consumer
   impact. It never bypasses syntax checks, metadata consistency or immutability.

JSON documents must declare a known draft. References must be self-contained local
JSON Pointers; missing targets, remote references, dynamic references and nested
resource IDs are rejected. The validator never downloads a schema from a URL in
an untrusted contract. Contract directories contain one schema and one metadata
file; imported project-local Protobuf files are not currently supported. Bundled
Protobuf well-known types can be imported.

## Safe Issue retries

In Actions, select **process-schema-issue → Run workflow → main**, then enter the
existing Issue number in `issue_number`. The Issue must have the `schema` label.

- Open/merged PR already linked: report it and make no changes. This preserves
  human edits and approvals; editing an Issue does not overwrite an existing PR.
- Generated branch exists but PR creation failed: validate the branch and resume
  PR creation without regenerating a new version or force-pushing.
- Closed unmerged PR, conflicting references, changed published content or an
  unrelated branch diff: stop for manual resolution. Reopen the existing PR or
  create a new Issue as appropriate.
- Runs for the same Issue are serialized. New runs use `schema/issue-{number}`;
  legacy `schema/{name}-vN` PRs are still recognized by their closing reference.

The automation uses the installed App and never merges its own PR. A successful
offline run does not prove that the App token, Registry or production consumers
work. Validate those separately in the intended environment.

## Automation identity

`issue-schema.yml`, `auto-pr.yml`, `report-pr.yml`, `tag-schema.yml` and
`release.yml` use the `hellnet-actions` installation token for their write
operations. The App slug and numeric user ID are resolved dynamically; automatic
tags use `hellnet-actions[bot]` and its `ID+slug[bot]@users.noreply.github.com`
address. No workflow stores a legacy custom address as the primary identity.

`validate-pr.yml`, `pr-check.yml`, `security.yml` and `codeql.yml` remain
read-only/native validator workflows. They use `GITHUB_TOKEN` only for checkout,
SARIF upload and existing GitHub Actions integrations. They never receive the App
private key. `report-pr.yml` runs after them in a trusted `workflow_run` context,
then uses the App token to update the single marked comment, the
`hellnet-actions / validation` check and focused labels. It does not approve or
merge PRs.

## Registry boundary

`check-compatibility.sh` uses Apicurio v2 GET of the artifact compatibility rule,
then its non-mutating **PUT `/test`** endpoint. The artifact must exist and have a
rule matching metadata. It refuses redirects and reports missing artifacts/rules,
authorization errors and violations as failures, never as successful checks.
It intentionally does not infer global rule inheritance or configure policies.

`register.sh` and `register-redpanda.sh --apply` are explicit remote writes. Do not
invoke them as part of ordinary local validation or a PR test. Redpanda defaults
to dry-run; creating topics additionally requires `--create-topics`.

References: [Apache Avro specification](https://avro.apache.org/docs/1.12.0/specification/),
[JSON Schema validation API](https://python-jsonschema.readthedocs.io/en/stable/validate/),
[Apicurio v2 API](https://www.apicur.io/registry/docs/apicurio-registry/2.6.x/assets-attachments/registry-rest-api.htm).
