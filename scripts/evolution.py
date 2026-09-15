"""Append-only history and conservative offline Avro compatibility gates."""
from __future__ import annotations

import argparse
import re
import subprocess
from pathlib import Path

from avro.compatibility import ReaderWriterCompatibilityChecker, SchemaCompatibilityType
from contracts import confined_path, require, validate_contract, validate_tree


def protect_history(repo, base):
    repo = Path(repo)
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|main|origin/main)", base):
        raise ValueError("base must be a full commit SHA, main or origin/main")
    resolved = subprocess.check_output(["git", "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}"], cwd=repo, text=True).strip()
    entries = subprocess.check_output(["git", "ls-tree", "-rz", resolved, "--", "schemas/"], cwd=repo).split(b"\0")
    published, old_paths = set(), set()
    for entry in filter(None, entries):
        info, filename = entry.split(b"\t", 1); mode, _, blob = info.decode().split(); relative = Path(filename.decode()); old_paths.add(relative)
        if relative.name == ".gitkeep": continue
        # One-time format retirement: JSON Schema and Protobuf are intentionally
        # removed by the Avro-only migration. All Avro history remains immutable.
        if len(relative.parts) > 1 and relative.parts[1] in {"json", "protobuf"}:
            continue
        published.add(relative.parent); current = repo / relative
        expected = subprocess.check_output(["git", "cat-file", "blob", blob], cwd=repo)
        require(mode in {"100644", "100755"} and current.is_file() and not current.is_symlink() and current.read_bytes() == expected, f"published file is immutable: {relative}")
    for directory in published:
        for path in (repo / directory).rglob("*"):
            require(not path.is_file() or path.relative_to(repo) in old_paths, f"cannot add files to a published version: {path.relative_to(repo)}")
    print(f"History protected against {resolved[:12]} ({len(published)} published versions)")


def check_pair(old, new, mode):
    if mode == "NONE": return
    pairs = []
    if mode in {"BACKWARD", "FULL"}: pairs.append((new, old, "BACKWARD"))
    if mode in {"FORWARD", "FULL"}: pairs.append((old, new, "FORWARD"))
    for reader, writer, direction in pairs:
        require(logical_types(reader) == logical_types(writer), "Avro logical-type changes require an explicit migration")
        result = ReaderWriterCompatibilityChecker().get_compatibility(reader, writer)
        require(result.compatibility == SchemaCompatibilityType.compatible, f"{direction} Avro incompatibility: {result.messages}")


def logical_types(schema, path="", seen=None):
    seen = set() if seen is None else seen
    if id(schema) in seen: return {}
    seen = seen | {id(schema)}; result = {}
    if schema.get_prop("logicalType"): result[path] = {key: schema.get_prop(key) for key in ("logicalType", "precision", "scale")}
    if schema.type == "record": children = [(field.name, field.type) for field in schema.fields]
    elif schema.type == "union": children = [(child.type, child) for child in schema.schemas]
    elif schema.type == "array": children = [("items", schema.items)]
    elif schema.type == "map": children = [("values", schema.values)]
    else: children = []
    for name, child in children: result.update(logical_types(child, path + "/" + name, seen))
    return result


def check_evolution(root):
    root, groups = Path(root), {}
    for path in sorted(root.rglob(".meta.json")): groups.setdefault(path.parent.parent, []).append(path.parent)
    for parent, versions in groups.items():
        versions.sort(key=lambda path: int(path.name[1:])); require([int(path.name[1:]) for path in versions] == list(range(1, len(versions) + 1)), f"versions must start at v1 without gaps: {parent}")
        for previous, current in zip(versions, versions[1:]):
            old_meta, old = validate_contract(previous, root); meta, new = validate_contract(current, root); mode = meta["compatibility"]
            if parent.relative_to(root).parts[:2] == ("avro", "fast"):
                require(old_meta["name"] != meta["name"], "Fast versions must have distinct identities")
                print(f"NEW IDENTITY: {meta['name']}; no cross-topic compatibility claim")
            else:
                check_pair(old, new, mode); print(f"{'EXPLICIT OPT-OUT' if mode == 'NONE' else mode}: {current}")
    print("Avro evolution policy satisfied")


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--root", type=Path, default=Path("schemas")); parser.add_argument("--base")
    args = parser.parse_args()
    try:
        args.root = confined_path(args.root, Path.cwd())
        if args.base: protect_history(Path.cwd(), args.base)
        validate_tree(args.root); check_evolution(args.root)
    except Exception as error: parser.exit(1, f"ERROR: {error}\n")


if __name__ == "__main__": main()
