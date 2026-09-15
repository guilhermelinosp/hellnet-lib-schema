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

CI installs the same pinned Avro dependencies and runs
the same suite. No credentials, running Registry, Kafka broker, Go module, containers
or generated application code are needed. Tests use temporary repositories and
mock GitHub/Registry responses; they never create real Issues, PRs or artifacts.

Run commands from the checkout root. CLI schema roots must resolve inside the
current directory; Registry checks accept only paths inside its `schemas/` tree.
Symlinks cannot escape these boundaries. `--base` accepts only a full commit SHA,
`main` or `origin/main`, never arbitrary revision expressions or Git options.

## Issue fields

The New Schema form accepts a YAML list of field mappings. The parser reads
heading-delimited sections, safely parses YAML and serializes Avro correctly.
Duplicate keys/names, aliases, unknown options, unsafe paths and malformed schemas
are errors. Generation validates in a temporary directory before publishing the
complete version directory. The shell entry point still emits `NAME`, `TYPE`,
`VERSION` and `PATH` as `KEY=value` lines.

| Option | Meaning |
|--------|---------|
| `name` | Canonical `fast-{domain}-{event}` identifier |
| `type` | An Avro type; never implicitly converted |
| `required` | Boolean, defaults to true; false creates a nullable union |
| `default` | A YAML value, not an already JSON-encoded string |
| `doc` | Description of a field |

- **Avro:** the generator preserves the Fast-only `fast-{domain}-{event}` naming
  policy. Fields accept primitive or structured Avro types. Optional fields generate a nullable
  union with a default. Defaults must match the first union branch. Existing
  logical types, names and defaults are validated, not merely JSON syntax.
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
   extended with extra files. The Avro-only migration is the sole exception: it
   retires the legacy `schemas/json` and `schemas/protobuf` trees. CI compares all
   remaining contents against the PR base SHA.
2. Fast Avro preserves `fast/{domain}/{event}/vN`, metadata/topic/subject
   `fast.{domain}.{event-as-dots}.vN`, namespace `fast.events.{domain}.vN` and
   record `Fast{Domain}{Event}VN`. A new N is a separate identity. CI prints
   `NEW IDENTITY`, not a misleading cross-version compatibility success. Producers
   and consumers must explicitly migrate to the new topic/contract.
3. Avro versions keep their metadata name across directory versions. The **new version's** metadata selects BACKWARD, FORWARD, FULL or
   NONE (`NO_CHECK` from the form maps to NONE). Checks compare adjacent versions,
   not all history; no transitive guarantee is claimed.
4. Avro uses Apache Avro reader/writer compatibility checks and also rejects
   logical-type changes.
6. NONE is a visible opt-out, not proof of safety. Review the migration and consumer
   impact. It never bypasses syntax checks, metadata consistency or immutability.

Avro documents are parsed with the pinned Apache Avro library. Defaults are
validated against their declared Avro types, including nullable unions and
logical types. Contract directories contain one `schema.avsc` and one metadata
file.

## Safe Issue retries

In Actions, select **process-schema-issue → Run workflow → main**, then enter the
existing Issue number in `issue-number`. The Issue must have the `schema` label.

- Open/merged PR already linked: report it and make no changes. This preserves
  human edits and approvals; editing an Issue does not overwrite an existing PR.
- Generated branch exists but PR creation failed: validate the branch and resume
  PR creation without regenerating a new version or force-pushing.
- Closed unmerged PR, conflicting references, changed published content or an
  unrelated branch diff: stop for manual resolution. Reopen the existing PR or
  create a new Issue as appropriate.
- Runs for the same Issue are serialized. New runs use `schema/issue-{number}`;
  legacy `schema/{name}-vN` PRs are still recognized by their closing reference.

The automation uses only the standard `GITHUB_TOKEN` and never requires a GitHub
App, private key or custom bot identity. A successful offline run does not prove
that the Registry or production consumers work; validate those separately.

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
[Apicurio v2 API](https://www.apicur.io/registry/docs/apicurio-registry/2.6.x/assets-attachments/registry-rest-api.htm).
