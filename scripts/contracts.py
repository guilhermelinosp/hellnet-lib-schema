"""Offline contract validation and deterministic Issue Form generation."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
import warnings
from pathlib import Path

import avro.schema
import grpc_tools
import jsonschema
import yaml
from google.protobuf import descriptor_pb2

FILES = {"avro": "schema.avsc", "json": "schema.json", "protobuf": "schema.proto"}
MODES = {"BACKWARD", "FORWARD", "FULL", "NONE"}
NAME = r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*"
FIELD = r"[A-Za-z_][A-Za-z0-9_]*"
DRAFT = "http://json-schema.org/draft-07/schema#"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"duplicate key: {key}")
        result[key] = value
    return result


def read_json(path):
    return json.loads(Path(path).read_text(), object_pairs_hook=unique_pairs,
                      parse_constant=lambda value: require(False, f"invalid JSON number: {value}"))


def dump_json(value):
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def pascal(value):
    return "".join(word[:1].upper() + word[1:] for word in re.split("[-_]", value))


def valid_avro_default(schema, value):
    kind = schema.type
    if kind == "null":
        return value is None
    if kind == "boolean":
        return type(value) is bool
    if kind in {"int", "long"}:
        bits = 32 if kind == "int" else 64
        return type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
    if kind in {"float", "double"}:
        return type(value) in {int, float} and math.isfinite(value)
    if kind == "string":
        return isinstance(value, str)
    if kind in {"bytes", "fixed"}:
        return isinstance(value, str) and all(ord(char) <= 255 for char in value) and (kind == "bytes" or len(value) == schema.size)
    if kind == "enum":
        return isinstance(value, str) and value in schema.symbols
    if kind == "union":
        # Repository policy: defaults match the first union branch.
        return valid_avro_default(schema.schemas[0], value)
    if kind == "array":
        return isinstance(value, list) and all(valid_avro_default(schema.items, child) for child in value)
    if kind == "map":
        return isinstance(value, dict) and all(isinstance(key, str) and valid_avro_default(schema.values, child)
                                              for key, child in value.items())
    if kind == "record":
        return (isinstance(value, dict) and not (set(value) - {field.name for field in schema.fields})
                and all((field.name in value or field.has_default) and valid_avro_default(
                    field.type, value[field.name] if field.name in value else field.default) for field in schema.fields))
    return False


def avro_defaults(schema, seen=None):
    """The Avro parser alone does not validate field defaults."""
    seen = set() if seen is None else seen
    if id(schema) in seen:
        return
    seen.add(id(schema))
    if schema.type == "record":
        for field in schema.fields:
            if field.has_default:
                require(valid_avro_default(field.type, field.default), f"invalid default for {field.name}")
            avro_defaults(field.type, seen)
    elif schema.type == "array":
        avro_defaults(schema.items, seen)
    elif schema.type == "map":
        avro_defaults(schema.values, seen)
    elif schema.type == "union":
        for child in schema.schemas:
            avro_defaults(child, seen)


def compile_proto(path):
    path = Path(path).resolve()
    with tempfile.TemporaryDirectory(prefix="schema-protoc-") as tmp:
        output = Path(tmp) / "schema.pb"
        result = subprocess.run(
            [sys.executable, "-m", "grpc_tools.protoc", f"-I{path.parent}",
             f"-I{Path(grpc_tools.__file__).parent / '_proto'}",
             f"--descriptor_set_out={output}", "--include_imports", path.name],
            capture_output=True, text=True, timeout=30, check=False,
        )
        require(result.returncode == 0, f"protoc: {result.stderr.strip()}")
        descriptors = descriptor_pb2.FileDescriptorSet.FromString(output.read_bytes())
    descriptor = next(item for item in descriptors.file if item.name == path.name)
    require(descriptor.syntax == "proto3", "only proto3 contracts are supported")
    require(bool(descriptor.message_type), "Protobuf must declare an event message")
    return descriptor


def validate_document(path, kind):
    if kind == "protobuf":
        return compile_proto(path)
    raw = read_json(path)
    require(isinstance(raw, dict), "schema must be an object")
    if kind == "avro":
        require(raw.get("type") == "record", "Avro top-level type must be record")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            parsed = avro.schema.parse(dump_json(raw))
        avro_defaults(parsed)
        return parsed
    validator = jsonschema.validators.validator_for(raw, default=None)
    require("$schema" in raw and validator is not None, "unknown or missing JSON Schema draft")
    validator.check_schema(raw)
    require(raw.get("type") == "object", "JSON event must have type object")
    # Contracts are self-contained: never fetch untrusted remote references.
    def refs(node):
        if isinstance(node, dict):
            require(not ({"$dynamicRef", "$recursiveRef"} & node.keys()), "dynamic/recursive refs are not supported")
            require(node is raw or not ({"$id", "id"} & node.keys()), "nested JSON resource IDs are not supported")
            if "$ref" in node:
                ref = node["$ref"]
                require(ref == "#" or ref.startswith("#/"), "only local JSON Pointer refs are supported")
                target = raw
                if ref != "#":
                    for part in ref[2:].split("/"):
                        key = part.replace("~1", "/").replace("~0", "~")
                        target = target[int(key)] if isinstance(target, list) else target[key]
                require(isinstance(target, (dict, bool)), f"invalid reference target: {ref}")
            for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
                for child in node.get(key, {}).values():
                    refs(child)
            for child in node.get("dependencies", {}).values():
                if isinstance(child, dict):
                    refs(child)
            for key in ("items", "additionalItems", "contains", "propertyNames", "additionalProperties",
                        "unevaluatedItems", "unevaluatedProperties", "not", "if", "then", "else",
                        "allOf", "anyOf", "oneOf", "prefixItems"):
                if key in node:
                    refs(node[key])
        elif isinstance(node, list):
            for child in node:
                refs(child)
    refs(raw)
    return raw


def validate_contract(directory, root):
    directory, root = Path(directory), Path(root)
    meta = read_json(directory / ".meta.json")
    require(isinstance(meta, dict), "metadata must be an object")
    kind, version, name = meta.get("type"), meta.get("version"), meta.get("name")
    require(kind in FILES, "invalid metadata type")
    require(type(version) is int and version > 0, "version must be a positive integer")
    require(meta.get("compatibility") in MODES, "invalid compatibility mode")
    require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)),
            "invalid metadata name")
    parts = directory.relative_to(root).parts
    require(parts[0] == kind and parts[-1] == f"v{version}", "metadata does not match path/type/version")
    present = [file for file in FILES.values() if (directory / file).is_file()]
    require(present == [FILES[kind]], "expected exactly one schema matching metadata type")
    parsed = validate_document(directory / FILES[kind], kind)
    if kind == "avro" and len(parts) > 1 and parts[1] == "fast":
        require(len(parts) == 5, "Fast path must be avro/fast/{domain}/{event}/vN")
        domain, event = parts[2:4]
        require(bool(re.fullmatch(r"[a-z0-9]+", domain)), "invalid Fast domain")
        require(bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", event)), "invalid Fast event")
        require(name == f"fast.{domain}.{event.replace('-', '.')}.v{version}", "invalid Fast metadata name")
        require(parsed.fullname == f"fast.events.{domain}.v{version}.Fast{pascal(domain + '-' + event)}V{version}",
                "invalid Fast Avro fullname")
    else:
        require(len(parts) == 3 and bool(re.fullmatch(NAME, parts[1])), "invalid generic contract path")
        require(not (kind == "avro" and parts[1].startswith("fast-")), "flat Fast Avro paths are forbidden")
        require(name == parts[1], "metadata name must match contract directory")
    return meta, parsed


def validate_tree(root):
    root = Path(root)
    require(root.is_dir(), f"schema root not found: {root}")
    for path in root.rglob("*"):
        require(not path.is_symlink(), f"symlinks are forbidden: {path}")
        if path.is_file() and path.name != ".gitkeep":
            require(path.name in {".meta.json", *FILES.values()}, f"unexpected contract file: {path}")
            require((path.parent / ".meta.json").is_file(), f"missing sibling metadata: {path}")
    contracts = sorted(root.rglob(".meta.json"))
    require(bool(contracts), "no schemas found")
    for path in contracts:
        try:
            meta, _ = validate_contract(path.parent, root)
            print(f"OK: {meta['name']} v{meta['version']} ({meta['type']})")
        except Exception as error:
            raise ValueError(f"{path.parent}: {error}") from error
    print(f"All schemas valid ({len(contracts)} contracts)")


class IssueLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys rather than silently overwriting a field."""


def yaml_mapping(loader, node):
    return unique_pairs((loader.construct_object(key), loader.construct_object(value))
                        for key, value in node.value)


IssueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def parse_issue(body):
    require(len(body) <= 65536, "Issue body exceeds 64 KiB")
    sections = {}
    heading, lines = None, []
    for line in body.replace("\r\n", "\n").splitlines():
        if line.startswith("### "):
            if heading is not None:
                require(heading not in sections, f"duplicate heading: {heading}")
                sections[heading] = "\n".join(lines).strip()
            heading, lines = line[4:].strip(), []
        else:
            lines.append(line)
    if heading is not None:
        require(heading not in sections, f"duplicate heading: {heading}")
        sections[heading] = "\n".join(lines).strip()
    name, kind = sections.get("Schema name", ""), sections.get("Format", "")
    mode = sections.get("Compatibility level", "BACKWARD")
    mode = "NONE" if mode == "NO_CHECK" else mode
    require(bool(re.fullmatch(NAME, name)), "invalid Schema name")
    require(kind in FILES, "Format must be avro, json or protobuf")
    require(mode in MODES, "invalid Compatibility level")
    fields_text = sections.get("Fields (YAML)", "")
    if fields_text.startswith("```"):
        lines = fields_text.splitlines()
        require(lines[0] in ("```", "```yaml", "```yml") and lines[-1] == "```", "invalid YAML code fence")
        fields_text = "\n".join(lines[1:-1])
    require(not any(isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
                    for token in yaml.scan(fields_text)), "YAML aliases/anchors are not supported")
    fields = yaml.load(fields_text, Loader=IssueLoader)
    require(isinstance(fields, list) and 0 < len(fields) <= 500, "Fields must be a list of 1..500 fields")
    names, numbers = set(), set()
    for field in fields:
        require(isinstance(field, dict), "each field must be a YAML mapping")
        require(not (set(field) - {"name", "type", "required", "default", "doc", "number"}), "unknown field option")
        field_name = field.get("name", "")
        require(isinstance(field_name, str) and bool(re.fullmatch(FIELD, field_name)), "invalid field name")
        require(field_name not in names, f"duplicate field: {field_name}")
        names.add(field_name)
        require(type(field.get("required", True)) is bool, "required must be true or false")
        require(isinstance(field.get("type"), (str, dict, list)), "field type is required")
        if "doc" in field:
            require(isinstance(field["doc"], str), "doc must be text")
        if kind == "protobuf":
            number = field.get("number")
            require(type(number) is int and 0 < number < 2**29 and not 19000 <= number <= 19999,
                    "Protobuf requires an explicit valid number for every field")
            require(number not in numbers, "duplicate Protobuf field number")
            numbers.add(number)
            require("default" not in field, "proto3 does not support explicit defaults")
        else:
            require("number" not in field, "number is only supported for Protobuf")
    return name, kind, mode, fields


def generate(body, root=Path("schemas")):
    name, kind, mode, fields = parse_issue(body)
    root = Path(root)
    fast = re.fullmatch(r"fast-([a-z0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)", name) if kind == "avro" else None
    require(kind != "avro" or fast is not None, "Avro generation requires fast-{domain}-{event}")
    relative = Path(kind) / (Path("fast", *fast.groups()) if fast else Path(name))
    parent = root / relative
    output_parents = [root, *(root.joinpath(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1))]
    require(not any(path.is_symlink() for path in output_parents), "symlinked output path")
    versions = [int(path.name[1:]) for path in parent.glob("v*") if re.fullmatch(r"v[1-9][0-9]*", path.name)]
    version = max(versions, default=0) + 1
    directory = parent / f"v{version}"
    metadata_name = f"fast.{fast[1]}.{fast[2].replace('-', '.')}.v{version}" if fast else name
    if kind == "avro":
        schema = {"type": "record", "name": f"Fast{pascal(fast[1] + '-' + fast[2])}V{version}" if fast else pascal(name),
                  "namespace": f"fast.events.{fast[1]}.v{version}" if fast else "hellnet.events.v1", "fields": []}
        for field in fields:
            ftype = field["type"]
            default = field.get("default")
            if not field.get("required", True):
                require(not isinstance(ftype, list), "optional Avro union: declare null in type and supply a default instead")
                ftype = ["null", ftype] if default is None else [ftype, "null"]
            item = {"name": field["name"], "type": ftype}
            if "default" in field or not field.get("required", True):
                item["default"] = default
            if "doc" in field:
                item["doc"] = field["doc"]
            schema["fields"].append(item)
        content = dump_json(schema)
    elif kind == "json":
        schema = {"$schema": DRAFT, "title": pascal(name), "type": "object", "properties": {}, "required": []}
        for field in fields:
            require(isinstance(field["type"], (str, list)), "JSON field type must be a type name or list")
            item = {"type": field["type"]}
            if "default" in field:
                item["default"] = field["default"]
                jsonschema.Draft7Validator(item).validate(field["default"])
            if "doc" in field:
                item["description"] = field["doc"]
            schema["properties"][field["name"]] = item
            if field.get("required", True):
                schema["required"].append(field["name"])
        content = dump_json(schema)
    else:
        scalars = {"double", "float", "int32", "int64", "uint32", "uint64", "sint32", "sint64",
                   "fixed32", "fixed64", "sfixed32", "sfixed64", "bool", "string", "bytes"}
        message_name, package, csharp = pascal(name), "hellnet.events.v1", "Hellnet.Events.V1"
        if version > 1:
            previous = compile_proto(parent / f"v{version - 1}" / FILES[kind])
            require(len(previous.message_type) == 1, "multiple Protobuf messages require manual evolution in a PR")
            message_name, package = previous.message_type[0].name, previous.package
            csharp = previous.options.csharp_namespace
        rows = ['syntax = "proto3";']
        if package:
            rows.append(f"package {package};")
        if csharp:
            rows.append(f"option csharp_namespace = {json.dumps(csharp)};")
        rows.extend(["", f"message {message_name} {{"])
        for field in fields:
            require(isinstance(field["type"], str) and field["type"] in scalars, "unsupported Protobuf scalar type")
            qualifier = "optional " if not field.get("required", True) else ""
            rows.append(f"  {qualifier}{field['type']} {field['name']} = {field['number']};")
        content = "\n".join([*rows, "}", ""])
    metadata = {"name": metadata_name, "type": kind, "version": version, "compatibility": mode,
                "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}
    # Validate before writing; rename the whole directory on the same filesystem.
    with tempfile.TemporaryDirectory(prefix="schema-generate-") as tmp:
        staging = Path(tmp) / relative / f"v{version}"
        staging.mkdir(parents=True)
        (staging / FILES[kind]).write_text(content)
        (staging / ".meta.json").write_text(dump_json(metadata))
        validate_contract(staging, Path(tmp))
        parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".schema-", dir=parent) as local_tmp:
            local = Path(local_tmp) / f"v{version}"
            shutil.copytree(staging, local)
            require(not directory.exists(), f"version already exists: {directory}")
            local.rename(directory)
    return {"NAME": name, "TYPE": kind, "VERSION": str(version), "PATH": str(directory / FILES[kind])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("root", nargs="?", default="schemas", type=Path)
    gen = sub.add_parser("generate")
    gen.add_argument("body", help="Issue body; use - to read stdin")
    gen.add_argument("--root", default="schemas", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "validate":
            validate_tree(args.root)
        else:
            body = sys.stdin.read() if args.body == "-" else args.body
            for key, value in generate(body, args.root).items():
                print(f"{key}={value}")
    except Exception as error:
        parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
