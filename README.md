# Hellnet Schema

Centralized event-contract repository and Schema Registry automation for event-driven services.

```
Issue → GitHub Actions → reviewed schema PR → Schema Registry
```

## How it works

```
Dev ──abre issue──► Issue Template ──Issue event──► Gera schema ──PR──► Review ──merge──► main
                                                                                             │
                                                                                         tag-schema
```

### Fluxo via Issue

1. Dev abre issue com template "New Schema"
2. GitHub Action `process-schema-issue` captura (`issues: opened`)
3. Script `generate-from-issue.sh` gera o schema no formato escolhido
4. Action cria branch, commita o schema, e abre um **Pull Request**
5. Time revisa o PR (diff do schema)
6. Ao merge na `main`, `tag-schema.yml` cria a tag imutável `schema/{nome}/v{versao}`
7. A sincronização com um Schema Registry é feita separadamente, pelo processo de registro correspondente

## Quick Start

### Creating a new schema

Open a new issue and fill the [template](.github/ISSUE_TEMPLATE/new-schema.yml):

```yaml
# What you fill in the issue:
Schema name: fast-order-created
Format: avro
Compatibility: BACKWARD
Fields:
  - name: orderId
    type: string
    required: true
  - name: amount
    type: double
    required: true
  - name: currency
    type: string
    default: "BRL"
```

After submitting:

1. GitHub Action generates the schema, creates the branch and opens a **Pull Request**
2. Team reviews the PR (schema diff)
3. When the PR is merged to `main`, `tag-schema.yml` creates the immutable schema tag
4. `Closes #<issue>` links the issue and closes it when the PR is merged

### Schema storage structure

Fast Avro contracts use the product/domain/event hierarchy:

```text
schemas/
└── avro/
    └── fast/
        └── {domain}/
            └── {event}/
                ├── v1/
                │   ├── schema.avsc
                │   └── .meta.json
                └── v2/
                    ├── schema.avsc
                    └── .meta.json
```

Example:

```text
schemas/avro/fast/ride/requested/v1/schema.avsc
schemas/avro/fast/ride/accepted/v1/schema.avsc
schemas/avro/fast/ride/completed/v1/schema.avsc
```

JSON Schema and Protobuf keep the generic layout `schemas/{format}/{schema-name}/v{version}`.

### Example schemas

| Schema | Format | File |
|--------|--------|------|
| Order Created | Avro (Fast) | `schemas/avro/fast/order/created/v1/schema.avsc` |
| Invoice Event | JSON | `schemas/json/hellnet-invoice-event/v1/schema.json` |
| Stock Updated | Protobuf | `schemas/protobuf/hellnet-stock-updated/v1/schema.proto` |

## Schema naming convention

Fast Avro contracts follow one canonical mapping:

```text
Issue schema name: fast-{domain}-{event}
Repository path:   schemas/avro/fast/{domain}/{event}/v{version}
Metadata name:     fast.{domain}.{event-as-dots}.v{version}
Avro namespace:    fast.events.{domain}.v{version}
Avro record:       Fast{Domain}{Event}V{version}
```

Examples:

```text
fast-ride-requested
→ schemas/avro/fast/ride/requested/v1
→ fast.ride.requested.v1
→ fast.events.ride.v1
→ FastRideRequestedV1

fast-driver-location-updated
→ schemas/avro/fast/driver/location-updated/v1
→ fast.driver.location.updated.v1
→ fast.events.driver.v1
→ FastDriverLocationUpdatedV1
```

The validator enforces these relationships, so a PR cannot place a Fast Avro contract in a flat `schemas/avro/fast-*` directory.

## Git tags

Each merged schema version receives an immutable tag after it reaches `main`:

```
schema/fast-ride-requested/v1
schema/fast-ride-completed/v1
schema/hellnet-invoice-event/v1
schema/hellnet-stock-updated/v1
```

## CI/CD Pipeline

| Workflow | Trigger | Action |
|----------|---------|--------|
| `issue-schema.yml` | Issue opened with `schema` label | Generates the schema branch and PR |
| `validate-pr.yml` | PR changing schemas, scripts or workflows; reusable call | Validates contracts, metadata, canonical layout and shell syntax |
| `pipeline.yml` | Push to `main` except workflow-only changes; manual run | Validates contracts and scripts, then publishes a repository semver release |
| `codeql.yml` | Push to `main`, PR or manual run | Analyzes GitHub Actions workflows |
| `security.yml` | PR or manual run | Runs Gitleaks and Trivy security scans |
| `tag-schema.yml` | Schema changes merged to `main` | Creates missing immutable schema tags |

This repository contains Avro, JSON Schema and Protobuf contracts plus shell tooling.
CI does not install Go or run Go builds, tests, vet, GoSec or govulncheck.
Repository semver releases are separate from immutable per-contract schema tags.

## Configuration

### GitHub Secrets (obrigatórios)

| Secret | Descrição |
|--------|-----------|
| `APICURIO_URL` | Apicurio Registry endpoint (ex: `http://192.168.1.254:8085`) |
| `APICURIO_TOKEN` | Token de autenticação (se exigido) |
| `HELLNET_ACTIONS_CLIENT_ID` | Client ID do GitHub App `hellnet-actions` |
| `HELLNET_ACTIONS_PRIVATE_KEY` | Private Key do App, armazenada como secret criptografado |

### Compatibility levels

| Level | Description |
|-------|-------------|
| `BACKWARD` | New schema can read data written with the previous |
| `FORWARD` | Old schema can read data written with the new |
| `FULL` | Both backward and forward compatible |
| `NONE` | No compatibility checks |

## Local development

### Validate schemas locally

```bash
./scripts/validate.sh
```

### Register schema manually

```bash
./scripts/register.sh \
  --registry "\$APICURIO_URL" \
  --group default \
  --schema schemas/avro/fast/ride/requested/v1
```

### Redpanda Schema Registry

The repository contains contracts, not event payloads. This script registers Avro
schemas only; it never publishes fake messages. Topic creation is opt-in and does
not publish events.

Prerequisites: `python3` and `curl`. `rpk` is optional and is used only with
`--apply --create-topics`.

```bash
# Safe default: no network writes (REDPANDA_SCHEMA_REGISTRY_URL defaults to
# http://localhost:8081; REDPANDA_BROKERS defaults to localhost:9092).
bash scripts/register-redpanda.sh --dry-run

# Register schemas (does not create topics or publish messages).
bash scripts/register-redpanda.sh --apply --registry http://localhost:8081

# Register and, only when rpk is installed, create the derived topics.
bash scripts/register-redpanda.sh --apply --create-topics
```

The endpoint is the Confluent-compatible Redpanda API:
`POST /subjects/{subject}/versions`, with content type
`application/vnd.schemaregistry.v1+json` and a JSON body whose `schema` value is
the complete Avro document. The stable mapping intentionally keeps subjects and
topics distinct:

| Schema directory | Subject | Topic |
|---|---|---|
| `fast/ride/requested/v1` | `fast.ride.requested.v1` | `fast.ride.requested.v1` |
| `fast/ride/accepted/v1` | `fast.ride.accepted.v1` | `fast.ride.accepted.v1` |
| `fast/driver/location-updated/v1` | `fast.driver.location.updated.v1` | `fast.driver.location.updated.v1` |

Fast Avro directories follow `fast/{domain}/{event}/v{version}`. Event names may
contain additional hyphen-separated words; metadata converts those event segments
to dots for the stable topic/subject name. A dry-run prints the schema, subject, endpoint, and topic without a
write. Registration is idempotent: submitting the same schema to the same
subject lets the registry deduplicate it. Schema Registry synchronization is
separate from publishing events; applications publish real payloads later using
the registered contracts.

## Related repos

| Repo | Purpose |
|------|---------|
| `hellnet-dep-kafka` | Kafka pub/sub library (consumes schemas) |
| `hellnet-dep-observability` | OpenTelemetry + logging |
