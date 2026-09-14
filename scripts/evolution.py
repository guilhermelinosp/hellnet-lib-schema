"""Append-only history and conservative offline compatibility gates."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from avro.compatibility import ReaderWriterCompatibilityChecker, SchemaCompatibilityType
from contracts import confined_path, require, validate_contract, validate_tree


def protect_history(repo, base):
    """Compare the checkout to the PR base, not only commits in the PR branch."""
    repo = Path(repo)
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|main|origin/main)", base):
        raise ValueError("base must be a full commit SHA, main or origin/main")
    resolved = subprocess.check_output(
        ["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], cwd=repo, text=True).strip()
    entries = subprocess.check_output(
        ["git", "ls-tree", "-rz", resolved, "--", "schemas/"], cwd=repo).split(b"\0")
    published = set()
    old_paths = set()
    for entry in filter(None, entries):
        info, filename = entry.split(b"\t", 1)
        mode, _, blob = info.decode().split()
        relative = Path(filename.decode())
        old_paths.add(relative)
        if relative.name == ".gitkeep":
            continue
        published.add(relative.parent)
        current = repo / relative
        expected = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=repo)
        require(mode in {"100644", "100755"} and current.is_file() and not current.is_symlink()
                and current.read_bytes() == expected, f"published file is immutable: {relative}")
    for directory in published:
        for path in (repo / directory).rglob("*"):
            require(not path.is_file() or path.relative_to(repo) in old_paths,
                    f"cannot add files to a published version: {path.relative_to(repo)}")
    print(f"History protected against {resolved[:12]} ({len(published)} published versions)")


def json_includes(reader, writer):
    """Sufficient (not complete) proof that reader accepts every writer value.

    Unknown/applicator constraints fail closed on changes; never sample payloads
    and claim a proof. Equality is safe for self-contained unchanged schemas.
    """
    if json.dumps(reader, sort_keys=True) == json.dumps(writer, sort_keys=True) or reader is True or reader == {} or writer is False:
        return True
    if not isinstance(reader, dict) or not isinstance(writer, dict):
        return False
    annotation = {"title", "description", "default", "examples", "$comment"}
    supported = annotation | {"$schema", "type", "properties", "required", "additionalProperties", "enum", "items"}
    # Complex contracts remain valid, but changing one requires a reviewed NONE
    # policy or a dedicated compatibility implementation.
    def supported_tree(node):
        if not isinstance(node, dict):
            return isinstance(node, bool)
        return not (set(node) - supported) and all(
            supported_tree(child) for child in node.get("properties", {}).values()
        ) and all(supported_tree(node[key]) for key in ("items", "additionalProperties") if key in node)
    if not supported_tree(reader) or not supported_tree(writer):
        return False
    if reader.get("$schema") != writer.get("$schema"):
        return False
    def types(node):
        value = node.get("type", ["null", "boolean", "integer", "number", "string", "object", "array"])
        values = {value} if isinstance(value, str) else set(value)
        return values | ({"integer"} if "number" in values else set())
    if not types(writer) <= types(reader):
        return False
    def enum_key(value):
        # Python's True == 1 must not establish JSON enum compatibility.
        return json.dumps(value, sort_keys=True)
    if "enum" in reader and ("enum" not in writer or not
                            {enum_key(v) for v in writer["enum"]} <= {enum_key(v) for v in reader["enum"]}):
        return False
    if "object" in types(writer):
        if not set(reader.get("required", [])) <= set(writer.get("required", [])):
            return False
        rp, wp = reader.get("properties", {}), writer.get("properties", {})
        ra, wa = reader.get("additionalProperties", True), writer.get("additionalProperties", True)
        if not json_includes(ra, wa):
            return False
        for key in rp.keys() | wp.keys():
            if not json_includes(rp.get(key, ra), wp.get(key, wa)):
                return False
    if "array" in types(writer) and not json_includes(reader.get("items", True), writer.get("items", True)):
        return False
    return True


def protobuf_additive(old, new):
    """Preserve existing API descriptors; permit independent additive fields.

    Intentionally stricter than binary-only compatibility (JSON names, options,
    presence, enums, oneofs and nested types must also remain unchanged).
    """
    old_shell, new_shell = type(old)(), type(new)()
    old_shell.CopyFrom(old)
    new_shell.CopyFrom(new)
    old_shell.ClearField("message_type")
    new_shell.ClearField("message_type")
    if old_shell != new_shell:
        return False
    messages = {message.name: message for message in new.message_type}
    for before in old.message_type:
        after = messages.get(before.name)
        if after is None:
            return False
        existing = {field.number: field for field in after.field}
        if any(existing.get(field.number) != field for field in before.field):
            return False
        old_numbers = {field.number for field in before.field}
        if any(field.HasField("oneof_index") for field in after.field if field.number not in old_numbers):
            return False
        a, b = type(before)(), type(after)()
        a.CopyFrom(before)
        b.CopyFrom(after)
        a.ClearField("field")
        b.ClearField("field")
        if a != b:
            return False
    return True


def check_pair(kind, old, new, mode):
    if mode == "NONE":
        return
    if kind == "protobuf":
        require(protobuf_additive(old, new), "Protobuf evolution must preserve existing descriptors and field numbers")
        return
    pairs = []
    if mode in {"BACKWARD", "FULL"}:
        pairs.append((new, old, "BACKWARD"))
    if mode in {"FORWARD", "FULL"}:
        pairs.append((old, new, "FORWARD"))
    for reader, writer, direction in pairs:
        if kind == "avro":
            require(logical_types(reader) == logical_types(writer), "Avro logical-type changes require an explicit migration")
            result = ReaderWriterCompatibilityChecker().get_compatibility(reader, writer)
            require(result.compatibility == SchemaCompatibilityType.compatible,
                    f"{direction} Avro incompatibility: {result.messages}")
        else:
            require(json_includes(reader, writer), f"{direction} JSON compatibility cannot be proven by the supported subset")


def logical_types(schema, path="", seen=None):
    seen = set() if seen is None else seen
    if id(schema) in seen:
        return {}
    seen = seen | {id(schema)}
    result = {}
    if schema.get_prop("logicalType"):
        result[path] = {key: schema.get_prop(key) for key in ("logicalType", "precision", "scale")}
    if schema.type == "record":
        children = [(field.name, field.type) for field in schema.fields]
    elif schema.type == "union":
        children = [(child.type, child) for child in schema.schemas]
    elif schema.type == "array":
        children = [("items", schema.items)]
    elif schema.type == "map":
        children = [("values", schema.values)]
    else:
        children = []
    for name, child in children:
        result.update(logical_types(child, path + "/" + name, seen))
    return result


def check_evolution(root):
    root = Path(root)
    groups = {}
    for path in sorted(root.rglob(".meta.json")):
        groups.setdefault(path.parent.parent, []).append(path.parent)
    for parent, versions in groups.items():
        versions.sort(key=lambda path: int(path.name[1:]))
        require([int(path.name[1:]) for path in versions] == list(range(1, len(versions) + 1)),
                f"versions must start at v1 without gaps: {parent}")
        for previous, current in zip(versions, versions[1:]):
            old_meta, old = validate_contract(previous, root)
            meta, new = validate_contract(current, root)
            mode = meta["compatibility"]
            if parent.relative_to(root).parts[:2] == ("avro", "fast"):
                require(old_meta["name"] != meta["name"], "Fast versions must have distinct identities")
                print(f"NEW IDENTITY: {meta['name']}; no cross-topic compatibility claim")
            else:
                check_pair(meta["type"], old, new, mode)
                print(f"{'EXPLICIT OPT-OUT' if mode == 'NONE' else mode}: {current}")
    print("Evolution policy satisfied")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("schemas"))
    parser.add_argument("--base", help="PR base commit; published files must remain byte-identical")
    args = parser.parse_args()
    try:
        args.root = confined_path(args.root, Path.cwd())
        if args.base:
            protect_history(Path.cwd(), args.base)
        validate_tree(args.root)
        check_evolution(args.root)
    except Exception as error:
        parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__":
    main()
