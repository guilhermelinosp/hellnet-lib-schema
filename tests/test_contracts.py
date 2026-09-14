"""Regression tests run without GitHub credentials or a Schema Registry."""
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import contracts as c
import evolution as e
import process_issue as p
import registry_check as r
import release as rel

REPO = Path(__file__).resolve().parents[1]


def setUpModule():
    # No developer-global hooks, signing, fsmonitor or ignore files in fixtures.
    environment = patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
    environment.start()
    unittest.addModuleCleanup(environment.stop)


def issue(kind="avro", fields="- name: id\n  type: string", name=None, mode="BACKWARD"):
    name = name or ("fast-ride-requested" if kind == "avro" else "hellnet-test-event")
    return (f"### Schema name\n\n{name}\n\n### Format\n\n{kind}\n\n"
            f"### Compatibility level\n\n{mode}\n\n### Fields (YAML)\n\n```yaml\n{fields}\n```\n\n"
            "### Description (optional)\n\nThis section must not become a field.\n")


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "schemas"

    def generate(self, *args, **kwargs):
        return Path(c.generate(issue(*args, **kwargs), self.root)["PATH"])

    def test_published_fixtures_are_valid(self):
        c.validate_tree(REPO / "schemas")

    def test_three_formats(self):
        for kind in c.FILES:
            fields = "- name: id\n  type: string" + ("\n  number: 1" if kind == "protobuf" else "")
            self.generate(kind, fields)
        c.validate_tree(self.root)

    def test_fast_multiword_and_version_mapping(self):
        for version in (1, 2):
            path = self.generate(name="fast-driver-location-updated")
            self.assertEqual(path.relative_to(self.root).as_posix(),
                             f"avro/fast/driver/location-updated/v{version}/schema.avsc")
            self.assertEqual(c.read_json(path)["name"], f"FastDriverLocationUpdatedV{version}")
            self.assertEqual(c.read_json(path.parent / ".meta.json")["name"], f"fast.driver.location.updated.v{version}")
        e.check_evolution(self.root)

    def test_generator_preserves_fast_only_avro_policy(self):
        with self.assertRaisesRegex(ValueError, "Avro generation requires fast"):
            self.generate(name="hellnet-order-created")

    def test_protobuf_generation_preserves_existing_message_identity(self):
        source = REPO / "schemas/protobuf/hellnet-stock-updated"
        shutil.copytree(source, self.root / "protobuf/hellnet-stock-updated")
        path = self.generate("protobuf", fields="- name: productId\n  type: string\n  number: 1", name="hellnet-stock-updated")
        self.assertIn("message StockUpdated", path.read_text())
        self.assertNotIn("message HellnetStockUpdated", path.read_text())

    def test_quotes_colons_unicode_and_multiline(self):
        path = self.generate(fields='- name: note\n  type: string\n  default: \'Olá: "test" | value\'\n  doc: |\n    First line\n    Second line')
        field = c.read_json(path)["fields"][0]
        self.assertEqual(field["default"], 'Olá: "test" | value')
        self.assertIn("Second line", field["doc"])

    def test_optional_avro_defaults(self):
        path = self.generate(fields="- name: id\n  type: string\n  required: false")
        field = c.read_json(path)["fields"][0]
        self.assertEqual(field["type"], ["null", "string"])
        self.assertIsNone(field["default"])

    def test_optional_non_null_default(self):
        path = self.generate(fields="- name: id\n  type: string\n  required: false\n  default: BRL")
        self.assertEqual(c.read_json(path)["fields"][0]["type"], ["string", "null"])

    def test_numeric_avro_default_and_boolean_distinction(self):
        self.generate(fields="- name: amount\n  type: double\n  default: 0")
        with self.assertRaisesRegex(ValueError, "invalid default"):
            self.generate(fields="- name: amount\n  type: int\n  default: true")

    def test_avro_nested_types(self):
        self.generate(fields="- name: items\n  type:\n    type: array\n    items: string\n  default: []")

    def test_no_check_normalization(self):
        path = self.generate(mode="NO_CHECK")
        self.assertEqual(c.read_json(path.parent / ".meta.json")["compatibility"], "NONE")

    def test_crlf_issue(self):
        c.generate(issue().replace("\n", "\r\n"), self.root)

    def test_invalid_forms_leave_no_version(self):
        cases = [
            issue(name="../../escape"), issue(name="fast-ride--created"), issue(kind="go"), issue(mode="ALL"),
            issue(fields="[]"), issue(fields="- name: x\n  type: string\n  type: int"),
            issue(fields="- name: x\n  type: string\n  unknown: true"),
            issue(fields="- name: x\n  type: string\n  required: 'false'"),
            issue(fields="- name: x\n  type: string\n- name: x\n  type: string"),
            issue(fields="- &field {name: x, type: string}\n- *field"),
            issue(fields="!!python/object/apply:os.system ['false']"),
            issue(fields="- name: x\n  type: missingType"),
            issue(fields="- name: x\n  type: int\n  default: bad"),
            issue("json", fields="- name: x\n  type: double"),
            issue("protobuf"), issue("protobuf", fields="- name: x\n  type: string\n  number: 19000"),
            issue("protobuf", fields="- name: x\n  type: string\n  number: 1\n  default: value"),
            issue() + "\n### Format\njson", "### Schema name\nfoo", "x" * 65537,
        ]
        for body in cases:
            with self.subTest(body=body[:100]), self.assertRaises(Exception):
                c.generate(body, self.root)
            self.assertFalse(list(self.root.rglob(".meta.json")))

    def test_malformed_format_documents(self):
        cases = [("avro", '{"type":"record","name":"Event","fields":[{"name":"x","type":"missing"}]}'),
                 ("avro", '{"type":"record","name":"Event","fields":[{"name":"x","type":"int","default":"bad"}]}'),
                 ("json", '{"$schema":"http://json-schema.org/draft-07/schema#","type":"object","properties":{"x":{"type":"oops"}}}'),
                 ("json", '{"$schema":"https://unknown.example/schema","type":"object"}'),
                 ("json", '{"type":"object","type":"object"}'),
                 ("protobuf", 'syntax = "proto3"; message X { string x = 1; string y = 1; }'),
                 ("protobuf", 'syntax = "proto3"; message X { Unknown x = 1; }'),
                 ("protobuf", 'syntax = "proto3"; nonsense')]
        for kind, content in cases:
            path = Path(self.tmp.name) / c.FILES[kind]
            path.write_text(content)
            with self.subTest(kind=kind, content=content), self.assertRaises(Exception):
                c.validate_document(path, kind)

    def test_json_refs_are_local_and_resolvable(self):
        path = self.generate("json")
        schema = c.read_json(path)
        for ref in ("https://example.invalid/schema", "#/missing"):
            schema["properties"]["id"] = {"$ref": ref}
            path.write_text(c.dump_json(schema))
            with self.assertRaises(Exception):
                c.validate_document(path, "json")
        schema["definitions"] = {"Id": {"type": "string"}}
        schema["properties"]["id"] = {"$ref": "#/definitions/Id"}
        path.write_text(c.dump_json(schema))
        c.validate_document(path, "json")

    def test_missing_metadata(self):
        path = self.generate()
        (path.parent / ".meta.json").unlink()
        with self.assertRaisesRegex(ValueError, "missing sibling"):
            c.validate_tree(self.root)

    def test_missing_schema(self):
        self.generate().unlink()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            c.validate_tree(self.root)

    def test_wrong_type_and_version_metadata(self):
        path = self.generate()
        meta_path = path.parent / ".meta.json"
        original = c.read_json(meta_path)
        for key, value in [("type", "json"), ("version", True), ("version", "1"), ("version", 0),
                           ("version", 2), ("name", "fast.wrong.v1"), ("compatibility", "NO_CHECK")]:
            meta = dict(original, **{key: value})
            meta_path.write_text(c.dump_json(meta))
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                c.validate_tree(self.root)

    def test_flat_fast_rejected(self):
        path = self.generate()
        target = self.root / "avro/fast-ride-requested/v1"
        target.parent.mkdir(parents=True)
        shutil.move(path.parent, target)
        with self.assertRaises(ValueError):
            c.validate_tree(self.root)

    def test_symlink_rejected(self):
        path = self.generate()
        (path.parent / "evil").symlink_to(path)
        with self.assertRaisesRegex(ValueError, "symlinks"):
            c.validate_tree(self.root)

    def test_unexpected_file_rejected(self):
        path = self.generate()
        (path.parent / "schema.txt").write_text("bad")
        with self.assertRaisesRegex(ValueError, "unexpected"):
            c.validate_tree(self.root)

    def test_version_gaps_rejected(self):
        self.generate()
        path = self.generate()
        shutil.rmtree(path.parent.parent / "v1")
        with self.assertRaisesRegex(ValueError, "without gaps"):
            e.check_evolution(self.root)

    def test_shell_cli_output(self):
        env = dict(os.environ, PATH=str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"])
        result = subprocess.run(["bash", str(REPO / "scripts/generate-from-issue.sh"), "-", "--root", str(self.root)],
                                input=issue(), text=True, capture_output=True, env=env, check=True, cwd=self.tmp.name)
        self.assertEqual({line.split("=", 1)[0] for line in result.stdout.splitlines()}, {"NAME", "TYPE", "VERSION", "PATH"})


class CompatibilityTests(unittest.TestCase):
    def test_json_boolean_is_not_numeric_enum(self):
        self.assertFalse(e.json_includes({"enum": [1]}, {"enum": [True]}))

    def test_avro_logical_type_change_rejected(self):
        def schema(logical):
            return c.avro.schema.parse(json.dumps({"type": "record", "name": "E", "fields": [
                {"name": "time", "type": {"type": "long", "logicalType": logical}}]}))
        with self.assertRaisesRegex(ValueError, "logical-type"):
            e.check_pair("avro", schema("timestamp-millis"), schema("timestamp-micros"), "FULL")

    def test_avro_backward_default_required(self):
        old = c.avro.schema.parse('{"type":"record","name":"E","fields":[]}')
        new = c.avro.schema.parse('{"type":"record","name":"E","fields":[{"name":"x","type":"string"}]}')
        with self.assertRaises(ValueError):
            e.check_pair("avro", old, new, "BACKWARD")
        e.check_pair("avro", old, new, "FORWARD")
        compatible = c.avro.schema.parse('{"type":"record","name":"E","fields":[{"name":"x","type":"string","default":""}]}')
        e.check_pair("avro", old, compatible, "FULL")

    def test_json_required_type_enum_and_open_objects(self):
        old = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        new = copy.deepcopy(old)
        new["properties"]["x"]["type"] = "number"
        e.check_pair("json", old, new, "BACKWARD")
        with self.assertRaises(ValueError):
            e.check_pair("json", old, new, "FULL")
        new = copy.deepcopy(old)
        new["required"].append("y")
        with self.assertRaises(ValueError):
            e.check_pair("json", old, new, "BACKWARD")
        new = copy.deepcopy(old)
        new["properties"]["y"] = {"type": "string"}
        # Previously y could have ANY type: even an optional field can narrow.
        with self.assertRaises(ValueError):
            e.check_pair("json", old, new, "BACKWARD")
        self.assertFalse(e.json_includes({"enum": [1]}, {"enum": [1, 2]}))

    def test_json_unknown_constraints_fail_closed(self):
        with self.assertRaises(ValueError):
            e.check_pair("json", {"type": "string", "pattern": "a"}, {"type": "string", "pattern": "b"}, "FULL")
        e.check_pair("json", {}, {"not": {}}, "NONE")

    def test_protobuf_additions_and_breakage(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "schema.proto"
            path.write_text('syntax = "proto3"; message E { string id = 1; }')
            old = c.compile_proto(path)
            path.write_text('syntax = "proto3"; message E { string id = 1; int32 count = 2; }')
            e.check_pair("protobuf", old, c.compile_proto(path), "FULL")
            for fields in ('string id = 2;', 'int32 id = 1;', 'string renamed = 1;', '',
                           'oneof choice { string id = 1; string other = 2; }'):
                path.write_text(f'syntax = "proto3"; message E {{ {fields} }}')
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    e.check_pair("protobuf", old, c.compile_proto(path), "BACKWARD")


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.path = Path(c.generate(issue(), self.repo / "schemas")["PATH"])
        self.git("init", "-b", "main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.invalid")
        self.git("add", "schemas")
        self.git("-c", "commit.gpgsign=false", "commit", "-m", "baseline")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True, stderr=subprocess.DEVNULL).strip()

    def test_unchanged_and_new_version_allowed(self):
        c.generate(issue(), self.repo / "schemas")
        e.protect_history(self.repo, self.base)

    def test_edit_blocked(self):
        self.path.write_text(self.path.read_text() + "\n")
        with self.assertRaisesRegex(ValueError, "immutable"):
            e.protect_history(self.repo, self.base)

    def test_delete_blocked(self):
        self.path.unlink()
        with self.assertRaisesRegex(ValueError, "immutable"):
            e.protect_history(self.repo, self.base)

    def test_metadata_edit_blocked(self):
        (self.path.parent / ".meta.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "immutable"):
            e.protect_history(self.repo, self.base)

    def test_addition_inside_published_version_blocked(self):
        (self.path.parent / "other.proto").write_text("anything")
        with self.assertRaisesRegex(ValueError, "cannot add"):
            e.protect_history(self.repo, self.base)

    def test_unknown_base_fails_closed(self):
        with self.assertRaises(ValueError):
            e.protect_history(self.repo, "not-a-ref")

    def test_base_option_injection_rejected(self):
        with self.assertRaises(ValueError):
            e.protect_history(self.repo, "--output=/tmp/not-a-revision")


class PathBoundaryTests(unittest.TestCase):
    def test_path_escape_and_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            boundary = Path(tmp) / "inside"
            boundary.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (boundary / "link").symlink_to(outside, target_is_directory=True)
            for path in (boundary / "../outside", boundary / "link/file", outside):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    c.confined_path(path, boundary)

    def test_registry_path_escape_stops_before_network(self):
        with patch.object(r, "request") as request:
            with self.assertRaises(ValueError):
                r.check("https://registry.example", "default", Path.cwd().parent)
            request.assert_not_called()


class IssueTests(unittest.TestCase):
    def test_generated_commit_uses_api_without_custom_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "schemas/new/schema.avsc"
            path.parent.mkdir(parents=True)
            path.write_text('{"type":"record","name":"Event","fields":[]}\n')
            responses = [{"tree": {"sha": "base-tree"}}, {"sha": "blob"}, {"sha": "tree"},
                         {"sha": "commit"}, {"ref": "refs/heads/schema/issue-12"},
                         {"commit": {"verification": {"verified": True}}}]
            with patch.dict(os.environ, {"GH_TOKEN": "test-token"}), patch.object(p, "api_request",
                                                                                       side_effect=responses) as request:
                old_cwd = Path.cwd()
                try:
                    os.chdir(root)
                    self.assertEqual(p.create_verified_commit("owner/repo", "schema/issue-12", "base",
                                                              ["schemas/new/schema.avsc"], "feat: generated",
                                                              "test-app"), "commit")
                finally:
                    os.chdir(old_cwd)
            commit_payload = request.call_args_list[3].args[2]
            self.assertNotIn("author", commit_payload)
            self.assertNotIn("committer", commit_payload)

    def test_existing_open_and_merged_prs_reused(self):
        for state in ("OPEN", "MERGED"):
            pr = {"body": "Closes #12", "state": state, "url": "https://github.com/owner/repo/pull/1"}
            self.assertEqual(p.linked_pr([pr], 12), pr)
            self.assertIsNone(p.linked_pr([pr], 1))

    def test_closed_or_ambiguous_pr_stops(self):
        with self.assertRaises(ValueError):
            p.linked_pr([{"body": "Closes #12", "state": "CLOSED"}], 12)
        with self.assertRaises(ValueError):
            p.linked_pr([{"body": "Closes #12"}, {"body": "Closes #12"}], 12)

    def test_existing_pr_does_not_mutate_git(self):
        responses = [json.dumps({"labels": [{"name": "schema"}]}),
                     json.dumps([{"body": "Closes #12", "state": "OPEN", "url": "existing"}])]
        with patch.object(p, "run", side_effect=responses) as run:
            self.assertEqual(p.process("owner/repo", 12, "bot", "123"), "existing")
            self.assertEqual(run.call_count, 2)

    def test_generate_then_resume_interrupted_pr_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            remote, checkout = Path(tmp) / "remote.git", Path(tmp) / "checkout"
            subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(remote)], check=True, capture_output=True)
            subprocess.run(["git", "clone", str(remote), str(checkout)], check=True, capture_output=True)
            old_cwd = Path.cwd()
            try:
                os.chdir(checkout)
                for args in [("config", "user.name", "Test"), ("config", "user.email", "test@example.invalid")]:
                    p.run("git", *args)
                (checkout / "README.md").write_text("Test repository")
                p.run("git", "add", "README.md")
                p.run("git", "-c", "commit.gpgsign=false", "commit", "-m", "baseline")
                p.run("git", "push", "origin", "main")
                real_run = p.run
                attempts = []
                def fake_gh(*args):
                    if args[0] != "gh":
                        return real_run(*args)
                    if args[1:3] == ("issue", "view"):
                        return json.dumps({"body": issue(), "labels": [{"name": "schema"}], "state": "OPEN"})
                    if args[1:3] == ("pr", "list"):
                        return "[]"
                    if args[1:3] == ("pr", "create"):
                        attempts.append(args)
                        if len(attempts) == 1:
                            raise RuntimeError("simulated GitHub outage after branch push")
                        return "https://github.com/owner/repo/pull/1"
                    raise AssertionError(args)
                with patch.object(p, "run", side_effect=fake_gh):
                    with self.assertRaisesRegex(RuntimeError, "outage"):
                        p.process("owner/repo", 12, "test-app", "123")
                    # Reproduce a fresh Actions checkout with the remote branch retained.
                    real_run("git", "checkout", "main")
                    real_run("git", "branch", "-D", "schema/issue-12")
                    self.assertIn("/pull/1", p.process("owner/repo", 12, "test-app", "123"))
                self.assertEqual(len(list((checkout / "schemas").rglob(".meta.json"))), 1)
            finally:
                os.chdir(old_cwd)


class RegistryTests(unittest.TestCase):
    def test_test_endpoint_does_not_modify_rules(self):
        directory = REPO / "schemas/avro/fast/ride/requested/v1"
        with patch.object(r, "request", side_effect=[(200, b'{"config":"BACKWARD"}'), (204, b"")]) as request:
            r.check("https://registry.example", "default", directory)
        self.assertTrue(request.call_args_list[0].args[0].endswith("/rules/COMPATIBILITY"))
        self.assertEqual(request.call_args_list[1].args[1], "PUT")
        self.assertTrue(request.call_args_list[1].args[0].endswith("/test"))

    def test_policy_mismatch_does_not_test_or_write(self):
        with patch.object(r, "request", return_value=(200, b'{"config":"NONE"}')) as request:
            with self.assertRaisesRegex(ValueError, "differs"):
                r.check("https://registry.example", "default", REPO / "schemas/avro/fast/ride/requested/v1")
        self.assertEqual(request.call_count, 1)

    def test_auth_failure_fails_closed(self):
        with patch.object(r, "request", side_effect=ValueError("Registry HTTP 401")):
            with self.assertRaisesRegex(ValueError, "401"):
                r.check("https://registry.example", "default", REPO / "schemas/avro/fast/ride/requested/v1")


class ReleaseTests(unittest.TestCase):
    def test_first_release(self):
        self.assertEqual(rel.next_version([], []), "v1.0.0")

    def test_feature_and_patch_release(self):
        self.assertEqual(rel.next_version(["v1.6.0"], ["feat: improve contract"]), "v1.7.0")
        self.assertEqual(rel.next_version(["v1.6.0"], ["fix: typo"]), "v1.6.1")

    def test_beta_release(self):
        self.assertEqual(rel.next_version(["v1.7.0-beta.2"], []), "v1.7.0-beta.3")

    def test_invalid_tags_ignored_and_no_major_bump(self):
        self.assertEqual(rel.next_version(["not-a-version", "v1.6.0"], ["refactor!: internal", "BREAKING CHANGE: none"]), "v1.6.1")


if __name__ == "__main__":
    unittest.main()
