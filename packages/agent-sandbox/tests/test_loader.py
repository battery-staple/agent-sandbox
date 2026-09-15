"""Tests for YAML loader and schema validation in agent_sandbox.config.loader."""
import os
import shutil
import sys
import tempfile
import unittest
import yaml

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agent_sandbox.config.loader import (
    load_engine_manifest,
    load_sandbox_config,
    save_sandbox_config,
    validate_manifest_dict,
)
from agent_sandbox.config.models import SandboxConfig
from agent_sandbox.cli import SandboxCLI


class TestLoader(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_loader_")
        self.schema_path = os.path.join(REPO_ROOT, "config", "engine-manifest.schema.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_load_repo_manifests(self):
        opencode_path = os.path.join(REPO_ROOT, "engines", "opencode", "manifest.yaml")
        manifest_opencode = load_engine_manifest(opencode_path, schema_path=self.schema_path)
        self.assertEqual(manifest_opencode.name, "opencode")
        self.assertEqual(manifest_opencode.port, 4096)
        self.assertEqual(manifest_opencode.web_url, "http://127.0.0.1:4096")
        self.assertIsNotNone(manifest_opencode.rules)
        self.assertEqual(manifest_opencode.rules.target_file, "~/.config/opencode/AGENTS.md")

        antigravity_path = os.path.join(REPO_ROOT, "engines", "antigravity", "manifest.yaml")
        manifest_anti = load_engine_manifest(antigravity_path, schema_path=self.schema_path)
        self.assertEqual(manifest_anti.name, "antigravity")
        self.assertEqual(manifest_anti.port, 58432)
        self.assertEqual(manifest_anti.web_url, "https://localhost:58432")
        self.assertIsNotNone(manifest_anti.skills)
        self.assertIn("~/.gemini/config/skills.json", manifest_anti.skills.catalogs)

    def test_validation_missing_required(self):
        with self.assertRaises(ValueError) as ctx:
            validate_manifest_dict({"name": "test", "port": 1234})
        self.assertIn("web_url", str(ctx.exception))

    def test_validation_invalid_name(self):
        with self.assertRaises(ValueError) as ctx:
            validate_manifest_dict({"name": "Test Engine!", "port": 1234, "web_url": "http://localhost"})
        self.assertIn("does not match", str(ctx.exception))

    def test_validation_invalid_port(self):
        with self.assertRaises(ValueError):
            validate_manifest_dict({"name": "test", "port": 70000, "web_url": "http://localhost"})
        with self.assertRaises(ValueError):
            validate_manifest_dict({"name": "test", "port": -5, "web_url": "http://localhost"})
        with self.assertRaises(ValueError):
            validate_manifest_dict({"name": "test", "port": True, "web_url": "http://localhost"})

    def test_validation_extra_properties(self):
        with self.assertRaises(ValueError) as ctx:
            validate_manifest_dict({
                "name": "test",
                "port": 1234,
                "web_url": "http://localhost",
                "unknown_prop": "error"
            })
        self.assertIn("was unexpected", str(ctx.exception))

    def test_parent_dir_mismatch(self):
        engine_dir = os.path.join(self.temp_dir, "dir_foo")
        os.makedirs(engine_dir)
        manifest_file = os.path.join(engine_dir, "manifest.yaml")
        with open(manifest_file, "w") as f:
            yaml.safe_dump({"name": "name_bar", "port": 1234, "web_url": "http://localhost"}, f)

        with self.assertRaises(ValueError) as ctx:
            load_engine_manifest(manifest_file, schema_path=self.schema_path)
        self.assertIn("does not match its parent directory", str(ctx.exception))

    def test_load_and_save_sandbox_config(self):
        cfg_file = os.path.join(self.temp_dir, "whitelist.yaml")
        initial = SandboxConfig(
            allowed_workspaces=("/Users/test/code",),
            allowed_commands={"git": {"allowed_args_regex": "status"}},
        )
        save_sandbox_config(initial, cfg_file)
        loaded = load_sandbox_config(cfg_file)
        self.assertEqual(loaded.allowed_workspaces, ("/Users/test/code",))
        self.assertIn("git", loaded.allowed_commands)

    def test_legacy_state_migrates_only_when_canonical_directory_is_absent(self):
        legacy_dir = os.path.join(self.temp_dir, "legacy")
        sandbox_dir = os.path.join(self.temp_dir, "canonical")
        os.makedirs(os.path.join(legacy_dir, "ipc"))
        with open(os.path.join(legacy_dir, "whitelist.yaml"), "w", encoding="utf-8") as f:
            f.write("allowed_workspaces: ['/tmp/project']\nallowed_commands: {}\n")

        cli = SandboxCLI(repo_root=REPO_ROOT, sandbox_dir=sandbox_dir)
        cli.migrate_legacy_state(legacy_dir=legacy_dir)
        self.assertTrue(os.path.isfile(os.path.join(sandbox_dir, "whitelist.yaml")))
        self.assertTrue(os.path.isdir(legacy_dir))

        with open(os.path.join(legacy_dir, "ignored.txt"), "w", encoding="utf-8") as f:
            f.write("not copied")
        cli.migrate_legacy_state(legacy_dir=legacy_dir)
        self.assertFalse(os.path.exists(os.path.join(sandbox_dir, "ignored.txt")))


if __name__ == "__main__":
    unittest.main()
