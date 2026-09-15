"""Tests for the typed compose-common parser in agent_sandbox.compose.common."""
import os
import shutil
import sys
import tempfile
import unittest
import yaml

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.compose.common import (
    ComposeBuildConfig,
    ComposeCommonConfig,
    ComposeResourceConfig,
    load_compose_common,
    parse_compose_common,
)


def _valid_fragment():
    return {
        "x-sandbox-common": {
            "image": "agent-sandbox:latest",
            "build": {
                "context": ".",
                "dockerfile": "Dockerfile.sandbox",
                "args": {"USER_UID": "${HOST_UID:-1000}"},
            },
            "restart": "unless-stopped",
            "shm_size": "2gb",
            "cap_drop": ["ALL"],
            "cap_add": ["CHOWN"],
            "deploy": {"resources": {"limits": {"cpus": "4.0", "memory": "8GB", "pids": 1024}}},
            "extra_hosts": ["host.docker.internal:host-gateway"],
            "ports": ["127.0.0.1:8080:8080"],
            "environment": ["HOST_EXEC_PORT=58433"],
        }
    }


class TestParseComposeCommon(unittest.TestCase):
    def test_parses_valid_fragment(self):
        cfg = parse_compose_common(_valid_fragment(), source="test.yaml")
        self.assertEqual(cfg.image, "agent-sandbox:latest")
        self.assertEqual(cfg.restart, "unless-stopped")
        self.assertEqual(cfg.shm_size, "2gb")
        self.assertEqual(cfg.cap_drop, ("ALL",))
        self.assertEqual(cfg.cap_add, ("CHOWN",))
        self.assertEqual(cfg.extra_hosts, ("host.docker.internal:host-gateway",))
        self.assertEqual(cfg.ports, ("127.0.0.1:8080:8080",))
        self.assertEqual(cfg.environment, ("HOST_EXEC_PORT=58433",))
        self.assertEqual(cfg.build, ComposeBuildConfig(context=".", dockerfile="Dockerfile.sandbox", args={"USER_UID": "${HOST_UID:-1000}"}))
        self.assertEqual(cfg.resources, ComposeResourceConfig(cpus="4.0", memory="8GB", pids=1024))

    def test_fails_fast_on_missing_top_level_key(self):
        data = _valid_fragment()
        del data["x-sandbox-common"]["image"]
        with self.assertRaises(ValueError) as ctx:
            parse_compose_common(data, source="test.yaml")
        self.assertIn("image", str(ctx.exception))

    def test_fails_fast_on_missing_nested_key(self):
        data = _valid_fragment()
        del data["x-sandbox-common"]["build"]["dockerfile"]
        with self.assertRaises(ValueError) as ctx:
            parse_compose_common(data, source="test.yaml")
        self.assertIn("dockerfile", str(ctx.exception))

    def test_fails_fast_on_missing_resources_limits(self):
        data = _valid_fragment()
        del data["x-sandbox-common"]["deploy"]["resources"]["limits"]["pids"]
        with self.assertRaises(ValueError) as ctx:
            parse_compose_common(data, source="test.yaml")
        self.assertIn("pids", str(ctx.exception))

    def test_fails_fast_on_wrong_type(self):
        data = _valid_fragment()
        data["x-sandbox-common"]["cap_drop"] = "ALL"
        with self.assertRaises(ValueError):
            parse_compose_common(data, source="test.yaml")


class TestLoadComposeCommon(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_common_")
        self.repo_root = os.path.join(self.temp_dir, "repo")
        os.makedirs(os.path.join(self.repo_root, "config"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _write(self, fragment):
        with open(os.path.join(self.repo_root, "config", "compose.common.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(fragment, f, sort_keys=False)

    def test_loads_and_resolves_fragment(self):
        self._write(_valid_fragment())
        cfg = load_compose_common(self.repo_root)
        self.assertIsInstance(cfg, ComposeCommonConfig)
        self.assertEqual(cfg.image, "agent-sandbox:latest")

    def test_missing_file_raises(self):
        with self.assertRaises(FileNotFoundError):
            load_compose_common(self.repo_root)


if __name__ == "__main__":
    unittest.main()
