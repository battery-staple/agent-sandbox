"""Tests for Antigravity engine adapter and BrowserMode configuration."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.engines import get_adapter
from agent_sandbox.engines.antigravity import (
    AntigravityAdapter,
    AntigravityUserConfig,
    BrowserMode,
    parse_antigravity_user_config,
)


class TestAntigravityAdapterConfig(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("antigravity")

    def test_default_config_returns_auto_mode(self):
        self.assertIsInstance(self.adapter, AntigravityAdapter)
        cfg = self.adapter.default_config()
        self.assertEqual(cfg.browser_mode, BrowserMode.AUTO)
        self.assertEqual(AntigravityUserConfig().browser_mode, BrowserMode.AUTO)
        self.assertEqual(cfg, AntigravityUserConfig())

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = parse_antigravity_user_config(Path(d) / "config.yaml")
            self.assertEqual(cfg, AntigravityUserConfig())
            self.assertEqual(cfg.browser_mode, BrowserMode.AUTO)

    def test_empty_and_null_files_return_default(self):
        with tempfile.TemporaryDirectory() as d:
            empty_file = Path(d) / "empty.yaml"
            empty_file.write_text("", encoding="utf-8")
            self.assertEqual(parse_antigravity_user_config(empty_file), AntigravityUserConfig())

            null_file = Path(d) / "null.yaml"
            null_file.write_text("null\n", encoding="utf-8")
            self.assertEqual(parse_antigravity_user_config(null_file), AntigravityUserConfig())

            dict_file = Path(d) / "dict.yaml"
            dict_file.write_text("{}\n", encoding="utf-8")
            self.assertEqual(parse_antigravity_user_config(dict_file), AntigravityUserConfig())

    def test_parse_valid_browser_modes(self):
        cases = [
            ("auto", BrowserMode.AUTO),
            ("host", BrowserMode.HOST),
            ("container", BrowserMode.CONTAINER),
        ]
        with tempfile.TemporaryDirectory() as d:
            for raw_val, expected_mode in cases:
                p = Path(d) / f"config_{raw_val}.yaml"
                p.write_text(f"browser_mode: {raw_val}\n", encoding="utf-8")
                cfg = parse_antigravity_user_config(p)
                self.assertEqual(cfg.browser_mode, expected_mode)
                self.assertEqual(self.adapter.parse_user_config(p).browser_mode, expected_mode)

    def test_reject_invalid_browser_mode(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("browser_mode: invalid_mode\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_antigravity_user_config(p)
            err_msg = str(ctx.exception)
            self.assertIn(f"Invalid browser_mode 'invalid_mode' in {p}", err_msg)
            self.assertIn("must be one of 'auto', 'host', 'container'", err_msg)

    def test_reject_invalid_browser_mode_types(self):
        with tempfile.TemporaryDirectory() as d:
            for val in [123, True, ""]:
                p = Path(d) / "config.yaml"
                p.write_text(f"browser_mode: {val}\n", encoding="utf-8")
                with self.assertRaises(ValueError) as ctx:
                    parse_antigravity_user_config(p)
                self.assertIn("must be one of 'auto', 'host', 'container'", str(ctx.exception))

    def test_reject_unexpected_keys(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("unexpected: 123\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_antigravity_user_config(p)
            self.assertIn("unexpected keys", str(ctx.exception))

            p.write_text("browser_mode: auto\nextra: foo\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_antigravity_user_config(p)
            self.assertIn("unexpected keys: ['extra']", str(ctx.exception))

    def test_reject_non_mapping(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("- auto\n- host\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_antigravity_user_config(p)
            self.assertIn("top level must be a mapping", str(ctx.exception))

    def test_compose_env(self):
        cases = [
            (BrowserMode.AUTO, ("BROWSER_MODE=auto",)),
            (BrowserMode.HOST, ("BROWSER_MODE=host",)),
            (BrowserMode.CONTAINER, ("BROWSER_MODE=container",)),
        ]
        for mode, expected_env in cases:
            cfg = AntigravityUserConfig(browser_mode=mode)
            self.assertEqual(self.adapter.compose_env(cfg), expected_env)

    def test_compose_env_type_check(self):
        with self.assertRaises(TypeError) as ctx:
            self.adapter.compose_env("not a config")
        self.assertIn("AntigravityAdapter.compose_env expects AntigravityUserConfig", str(ctx.exception))

    def test_status_lines(self):
        cases = [
            (BrowserMode.AUTO, ("Browser mode: auto",)),
            (BrowserMode.HOST, ("Browser mode: host",)),
            (BrowserMode.CONTAINER, ("Browser mode: container",)),
        ]
        for mode, expected_status in cases:
            cfg = AntigravityUserConfig(browser_mode=mode)
            self.assertEqual(self.adapter.status_lines(cfg), expected_status)

    def test_status_lines_type_check(self):
        with self.assertRaises(TypeError) as ctx:
            self.adapter.status_lines("not a config")
        self.assertIn("AntigravityAdapter.status_lines expects AntigravityUserConfig", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
