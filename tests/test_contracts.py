"""Regression tests for the Avro-only contract flow."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import contracts as c
import evolution as e

REPO = Path(__file__).resolve().parents[1]


def issue(name="fast-ride-requested", kind="avro", fields="- name: id\n  type: string", mode="BACKWARD"):
    return (f"### Schema name\n\n{name}\n\n### Format\n\n{kind}\n\n"
            f"### Compatibility level\n\n{mode}\n\n### Fields (YAML)\n\n```yaml\n{fields}\n```\n")


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "schemas"

    def generate(self, **kwargs):
        return Path(c.generate(issue(**kwargs), self.root)["PATH"])

    def test_repository_contains_only_valid_avro(self):
        c.validate_tree(REPO / "schemas")
        self.assertFalse(list((REPO / "schemas").glob("json/**")))
        self.assertFalse(list((REPO / "schemas").glob("protobuf/**")))

    def test_only_avro_is_supported(self):
        self.assertEqual(c.FILES, {"avro": "schema.avsc"})
        with self.assertRaisesRegex(ValueError, "Format must be avro"):
            c.generate(issue(kind="json"), self.root)
        with self.assertRaisesRegex(ValueError, "Format must be avro"):
            c.generate(issue(kind="protobuf"), self.root)

    def test_fast_generation_and_metadata(self):
        path = self.generate(name="fast-driver-location-updated")
        self.assertEqual(path.relative_to(self.root).as_posix(), "avro/fast/driver/location-updated/v1/schema.avsc")
        schema, metadata = c.read_json(path), c.read_json(path.parent / ".meta.json")
        self.assertEqual(schema["name"], "FastDriverLocationUpdatedV1")
        self.assertEqual(metadata["name"], "fast.driver.location.updated.v1")
        self.assertEqual(metadata["type"], "avro")

    def test_optional_default_and_docs(self):
        path = self.generate(fields="- name: note\n  type: string\n  required: false\n  doc: Optional note")
        field = c.read_json(path)["fields"][0]
        self.assertEqual(field["type"], ["null", "string"])
        self.assertIsNone(field["default"])
        self.assertEqual(field["doc"], "Optional note")

    def test_invalid_issue_never_writes_version(self):
        for body in (issue(name="fast-ride--created"), issue(name="hellnet-event"), issue(fields="[]"), issue(fields="- name: id\n  type: string\n  unknown: true")):
            with self.assertRaises(Exception): c.generate(body, self.root)
            self.assertFalse(list(self.root.rglob(".meta.json")))

    def test_metadata_and_files_are_strict(self):
        path = self.generate()
        metadata = path.parent / ".meta.json"
        data = c.read_json(metadata); data["type"] = "json"; metadata.write_text(c.dump_json(data))
        with self.assertRaisesRegex(ValueError, "metadata type"):
            c.validate_tree(self.root)
        metadata.write_text(c.dump_json(dict(data, type="avro")))
        path.with_name("schema.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            c.validate_tree(self.root)

    def test_versions_and_evolution(self):
        self.generate(); self.generate()
        c.validate_tree(self.root); e.check_evolution(self.root)
        shutil.rmtree(self.root / "avro/fast/ride/requested/v1")
        with self.assertRaisesRegex(ValueError, "without gaps"):
            e.check_evolution(self.root)

    def test_avro_compatibility(self):
        old = c.avro.schema.parse('{"type":"record","name":"E","fields":[]}')
        new = c.avro.schema.parse('{"type":"record","name":"E","fields":[{"name":"x","type":"string"}]}')
        with self.assertRaises(ValueError): e.check_pair(old, new, "BACKWARD")
        compatible = c.avro.schema.parse('{"type":"record","name":"E","fields":[{"name":"x","type":"string","default":""}]}')
        e.check_pair(old, compatible, "FULL")


if __name__ == "__main__": unittest.main()
