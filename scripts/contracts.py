"""Validate and generate the repository's Avro event contracts."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import shutil
import tempfile
from pathlib import Path

import avro.schema
import yaml

FILES = {"avro": "schema.avsc"}
MODES = {"BACKWARD", "FORWARD", "FULL", "NONE"}
NAME = r"[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*"
FIELD = r"[A-Za-z_][A-Za-z0-9_]*"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def confined_path(path, boundary):
    boundary = Path(boundary).resolve()
    candidate = Path(path).resolve()
    require(candidate.is_relative_to(boundary), f"path must stay inside {boundary}")
    return candidate


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
    if kind == "null": return value is None
    if kind == "boolean": return type(value) is bool
    if kind in {"int", "long"}:
        bits = 32 if kind == "int" else 64
        return type(value) is int and -(2 ** (bits - 1)) <= value < 2 ** (bits - 1)
    if kind in {"float", "double"}: return type(value) in {int, float} and math.isfinite(value)
    if kind in {"bytes", "fixed"}:
        return isinstance(value, str) and all(ord(char) <= 255 for char in value)
    if kind == "enum": return isinstance(value, str) and value in schema.symbols
    if kind == "union": return valid_avro_default(schema.schemas[0], value)
    if kind == "array": return isinstance(value, list) and all(valid_avro_default(schema.items, child) for child in value)
    if kind == "map": return isinstance(value, dict) and all(isinstance(key, str) and valid_avro_default(schema.values, child) for key, child in value.items())
    if kind == "record":
        names = {field.name for field in schema.fields}
        return isinstance(value, dict) and not (set(value) - names) and all(
            (field.name in value or field.has_default) and valid_avro_default(field.type, value.get(field.name, field.default))
            for field in schema.fields)
    return False


def avro_defaults(schema, seen=None):
    seen = set() if seen is None else seen
    if id(schema) in seen: return
    seen.add(id(schema))
    if schema.type == "record":
        for field in schema.fields:
            if field.has_default: require(valid_avro_default(field.type, field.default), f"invalid default for {field.name}")
            avro_defaults(field.type, seen)
    elif schema.type == "array": avro_defaults(schema.items, seen)
    elif schema.type == "map": avro_defaults(schema.values, seen)
    elif schema.type == "union":
        for child in schema.schemas: avro_defaults(child, seen)


def validate_document(path, kind="avro"):
    require(kind == "avro", "only Avro contracts are supported")
    raw = read_json(path)
    require(isinstance(raw, dict) and raw.get("type") == "record", "Avro top-level type must be record")
    parsed = avro.schema.parse(dump_json(raw))
    avro_defaults(parsed)
    return parsed


def validate_contract(directory, root):
    directory, root = Path(directory), Path(root)
    meta = read_json(directory / ".meta.json")
    kind, version, name = meta.get("type"), meta.get("version"), meta.get("name")
    require(kind == "avro", "metadata type must be avro")
    require(type(version) is int and version > 0, "version must be a positive integer")
    require(meta.get("compatibility") in MODES, "invalid compatibility mode")
    require(isinstance(name, str) and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name)), "invalid metadata name")
    parts = directory.relative_to(root).parts
    require(parts[0] == "avro" and parts[-1] == f"v{version}", "metadata does not match path/type/version")
    require([file for file in FILES.values() if (directory / file).is_file()] == [FILES["avro"]],
            "expected exactly one Avro schema file")
    parsed = validate_document(directory / FILES["avro"])
    if parts[1:2] == ("fast",):
        require(len(parts) == 5, "Fast path must be avro/fast/{domain}/{event}/vN")
        domain, event = parts[2:4]
        require(bool(re.fullmatch(r"[a-z0-9]+", domain)), "invalid Fast domain")
        require(bool(re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", event)), "invalid Fast event")
        require(name == f"fast.{domain}.{event.replace('-', '.')}.v{version}", "invalid Fast metadata name")
        require(parsed.fullname == f"fast.events.{domain}.v{version}.Fast{pascal(domain + '-' + event)}V{version}", "invalid Fast Avro fullname")
    else:
        require(len(parts) == 3 and bool(re.fullmatch(NAME, parts[1])), "invalid generic Avro contract path")
        require(name == parts[1], "metadata name must match contract directory")
    return meta, parsed


def validate_tree(root):
    root = Path(root)
    require(root.is_dir(), f"schema root not found: {root}")
    for path in root.rglob("*"):
        require(not path.is_symlink(), f"symlinks are forbidden: {path}")
        if path.is_file() and path.name != ".gitkeep":
            require(path.name in {".meta.json", "schema.avsc"}, f"unexpected contract file: {path}")
            require((path.parent / ".meta.json").is_file(), f"missing sibling metadata: {path}")
    contracts = sorted(root.rglob(".meta.json"))
    require(bool(contracts), "no schemas found")
    for path in contracts:
        meta, _ = validate_contract(path.parent, root)
        print(f"OK: {meta['name']} v{meta['version']} (avro)")
    print(f"All Avro schemas valid ({len(contracts)} contracts)")


class IssueLoader(yaml.SafeLoader): pass


def yaml_mapping(loader, node):
    return unique_pairs((loader.construct_object(key), loader.construct_object(value)) for key, value in node.value)


IssueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, yaml_mapping)


def parse_issue(body):
    require(len(body) <= 65536, "Issue body exceeds 64 KiB")
    sections, heading, lines = {}, None, []
    for line in body.replace("\r\n", "\n").splitlines():
        if line.startswith("### "):
            if heading is not None: sections[heading] = "\n".join(lines).strip()
            heading, lines = line[4:].strip(), []
        else: lines.append(line)
    if heading is not None: sections[heading] = "\n".join(lines).strip()
    name, kind = sections.get("Schema name", ""), sections.get("Format", "")
    mode = sections.get("Compatibility level", "BACKWARD")
    mode = "NONE" if mode == "NO_CHECK" else mode
    require(bool(re.fullmatch(r"fast-[a-z0-9]+-[a-z0-9]+(?:-[a-z0-9]+)*", name)), "Schema name must be fast-{domain}-{event}")
    require(kind == "avro", "Format must be avro")
    require(mode in MODES, "invalid Compatibility level")
    fields_text = sections.get("Fields (YAML)", "")
    if fields_text.startswith("```"):
        lines = fields_text.splitlines(); require(lines[0] in ("```", "```yaml", "```yml") and lines[-1] == "```", "invalid YAML code fence")
        fields_text = "\n".join(lines[1:-1])
    fields = yaml.load(fields_text, Loader=IssueLoader)
    require(isinstance(fields, list) and 0 < len(fields) <= 500, "Fields must be a list of 1..500 fields")
    names = set()
    for field in fields:
        require(isinstance(field, dict) and not (set(field) - {"name", "type", "required", "default", "doc"}), "invalid field options")
        field_name = field.get("name", "")
        require(isinstance(field_name, str) and bool(re.fullmatch(FIELD, field_name)) and field_name not in names, "invalid or duplicate field name")
        names.add(field_name)
        require(type(field.get("required", True)) is bool and isinstance(field.get("type"), (str, dict, list)), "invalid field definition")
        if "doc" in field: require(isinstance(field["doc"], str), "doc must be text")
    return name, kind, mode, fields


def generate(body, root=Path("schemas")):
    name, kind, mode, fields = parse_issue(body)
    domain, event = re.fullmatch(r"fast-([a-z0-9]+)-([a-z0-9]+(?:-[a-z0-9]+)*)", name).groups()
    relative = Path("avro") / "fast" / domain / event
    parent, version = Path(root) / relative, 1
    versions = [int(path.name[1:]) for path in parent.glob("v*") if re.fullmatch(r"v[1-9][0-9]*", path.name)]
    version = max(versions, default=0) + 1
    directory = parent / f"v{version}"
    schema = {"type": "record", "name": f"Fast{pascal(domain + '-' + event)}V{version}", "namespace": f"fast.events.{domain}.v{version}", "fields": []}
    for field in fields:
        ftype, default = field["type"], field.get("default")
        if not field.get("required", True): ftype = ["null", ftype] if default is None else [ftype, "null"]
        item = {"name": field["name"], "type": ftype}
        if "default" in field or not field.get("required", True): item["default"] = default
        if "doc" in field: item["doc"] = field["doc"]
        schema["fields"].append(item)
    with tempfile.TemporaryDirectory(prefix="schema-generate-") as tmp:
        staging = Path(tmp) / relative / f"v{version}"; staging.mkdir(parents=True)
        (staging / "schema.avsc").write_text(dump_json(schema))
        (staging / ".meta.json").write_text(dump_json({"name": f"fast.{domain}.{event.replace('-', '.')}.v{version}", "type": "avro", "version": version, "compatibility": mode, "createdAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")}))
        validate_contract(staging, Path(tmp)); parent.mkdir(parents=True, exist_ok=True)
        require(not directory.exists(), f"version already exists: {directory}"); shutil.copytree(staging, directory)
    return {"NAME": name, "TYPE": kind, "VERSION": str(version), "PATH": str(directory / "schema.avsc")}


def main():
    parser = argparse.ArgumentParser(); sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate"); validate.add_argument("root", nargs="?", default="schemas", type=Path)
    gen = sub.add_parser("generate"); gen.add_argument("body"); gen.add_argument("--root", default="schemas", type=Path)
    args = parser.parse_args()
    try:
        args.root = confined_path(args.root, Path.cwd())
        if args.command == "validate": validate_tree(args.root)
        else:
            body = __import__("sys").stdin.read() if args.body == "-" else args.body
            for key, value in generate(body, args.root).items(): print(f"{key}={value}")
    except Exception as error: parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__": main()
