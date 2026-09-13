# Hellnet Schema

Centralized event-contract repository and Schema Registry automation for event-driven services.

```
Issue → GitHub Actions → reviewed schema PR → Schema Registry
```

## How it works

```
Dev ──abre issue──► Issue Template ──webhook──► Gera schema ──PR──► Review ──merge──► Apicurio Registry
                        ▲                                                        │
                        └────────────────── git tag ─────────────────────────────┘
```

### Fluxo via Issue (webhook)

1. Dev abre issue com template "New Schema"
2. GitHub Action `process-schema-issue` captura (`issues: opened`)
3. Script `generate-from-issue.sh` gera o schema no formato escolhido
4. Action cria branch, commita o schema, e abre um **Pull Request**
5. Time revisa o PR (diff do schema)
6. Ao merge na `main`, workflow `register-apicurio`:
   - Valida compatibilidade com versão anterior
   - Registra no Apicurio Registry
   - Cria tag `schema/{nome}/v{versao}`

1. **Dev opens an Issue** using the "New Schema" template
2. **GitHub Action** captures the issue, generates the schema file, commits and tags
3. **On merge** to `main`, schema is registered in Apicurio Registry automatically
4. **Hellnet.Kafka** consumes the schema from Registry to serialize/deserialize messages

## Quick Start

### Creating a new schema

Open a new issue and fill the [template](.github/ISSUE_TEMPLATE/new-schema.yml):

```yaml
# What you fill in the issue:
Schema name: hellnet-order-created
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

1. Webhook triggers → schema is generated and committed
2. Tag created: `schema/hellnet-order-created/v1`
3. Issue is closed with reference to the schema file
4. PR is created automatically (or you create one to review)
5. On merge to `main`, schema is registered in Apicurio

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
| Order Created | Avro | `schemas/avro/hellnet-order-created/v1/schema.avsc` |
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
schema/hellnet-order-created/v1
schema/hellnet-invoice-event/v2
schema/hellnet-stock-updated/v1
```

## CI/CD Pipeline

| Workflow | Trigger | Action |
|----------|---------|--------|
| `issue-schema.yml` | Issue opened with `schema` label | Generates the schema branch and PR |
| `validate-pr.yml` | PR with changes in `schemas/` | Validates syntax, metadata and canonical layout |
| `tag-schema.yml` | Schema changes merged to `main` | Creates missing immutable schema tags |

## Configuration

### GitHub Secrets (obrigatórios)

| Secret | Descrição |
|--------|-----------|
| `APICURIO_URL` | Apicurio Registry endpoint (ex: `http://192.168.1.254:8085`) |
| `APICURIO_TOKEN` | Token de autenticação (se exigido) |

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
  --schema schemas/avro/hellnet-order-created/v1
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
| `hellnet-order-created/v1` | `hellnet-order-created` | `hellnet.order.created.v1` |

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
