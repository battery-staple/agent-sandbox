"""Tests for modular engine adapters (opencode + antigravity) and parse-once runtimes."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.engines import (
    EngineRuntime,
    get_adapter,
    load_all_runtimes,
    runtimes_for_active,
)
from agent_sandbox.engines.antigravity import AntigravityUserConfig, parse_antigravity_user_config
from agent_sandbox.engines.opencode import OpencodeUserConfig, parse_opencode_user_config


class TestOpencodeAdapter(unittest.TestCase):
    def test_missing_file_returns_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = parse_opencode_user_config(Path(d) / "config.yaml")
            self.assertEqual(cfg, OpencodeUserConfig())
            self.assertEqual(cfg.server.username, "opencode")
            self.assertIsNone(cfg.server.password)

    def test_valid_password_parsed_once(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("server:\n  username: alice\n  password: s3cret\n", encoding="utf-8")
            cfg = parse_opencode_user_config(p)
            self.assertEqual(cfg.server.username, "alice")
            self.assertEqual(cfg.server.password, "s3cret")

    def test_empty_password_means_generated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("server:\n  username: opencode\n  password: ''\n", encoding="utf-8")
            cfg = parse_opencode_user_config(p)
            self.assertIsNone(cfg.server.password)

    def test_unknown_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("bogus: 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_opencode_user_config(p)

    def test_compose_env_and_status_zero_redaction(self):
        adapter = get_adapter("opencode")
        cfg = OpencodeUserConfig()
        self.assertIn("OPENCODE_SERVER_USERNAME=opencode", adapter.compose_env(cfg))
        self.assertFalse(any(e.startswith("OPENCODE_SERVER_PASSWORD=") for e in adapter.compose_env(cfg)))
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("server:\n  username: alice\n  password: s3cret\n", encoding="utf-8")
            cfg2 = parse_opencode_user_config(p)
            self.assertIn("OPENCODE_SERVER_PASSWORD=s3cret", adapter.compose_env(cfg2))
            # Zero redaction: password value shown in status.
            self.assertTrue(any("s3cret" in line for line in adapter.status_lines(cfg2)))


class TestAntigravityAdapter(unittest.TestCase):
    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = parse_antigravity_user_config(Path(d) / "config.yaml")
            self.assertEqual(cfg, AntigravityUserConfig())

    def test_non_empty_rejected_for_now(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("anything: 1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_antigravity_user_config(p)

    def test_compose_env_default(self):
        adapter = get_adapter("antigravity")
        self.assertEqual(adapter.compose_env(AntigravityUserConfig()), ("BROWSER_MODE=auto",))


class TestRuntimes(unittest.TestCase):
    def test_load_all_and_filter_without_reparsing(self):
        from agent_sandbox.config.models import EngineManifest

        with tempfile.TemporaryDirectory() as d:
            sb = Path(d) / "sandbox"
            (sb / "opencode").mkdir(parents=True)
            (sb / "opencode" / "config.yaml").write_text(
                "server:\n  username: alice\n  password: pw\n", encoding="utf-8"
            )
            manifests = [
                EngineManifest(name="opencode", port=4096, web_url="http://127.0.0.1:4096"),
                EngineManifest(name="antigravity", port=58432, web_url="https://localhost:58432"),
            ]
            all_rts = load_all_runtimes(manifests, sb)
            self.assertEqual(len(all_rts), 2)
            active = runtimes_for_active([manifests[0]], all_rts)
            self.assertEqual(len(active), 1)
            self.assertEqual(active[0].manifest.name, "opencode")
            self.assertEqual(active[0].config.server.password, "pw")


if __name__ == "__main__":
    unittest.main()
