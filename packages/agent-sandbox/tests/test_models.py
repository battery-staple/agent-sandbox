"""Tests for immutable data models in agent_sandbox.config.models."""
import unittest
from dataclasses import FrozenInstanceError

import os
import sys
PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.config.models import (
    EngineManifest,
    EngineRuleConfig,
    EngineSkillConfig,
    SandboxConfig,
)


class TestModels(unittest.TestCase):
    def test_engine_rule_config_immutability(self):
        cfg = EngineRuleConfig(target_file="~/.config/opencode/AGENTS.md", host_sources=("~/AGENTS.md",))
        self.assertEqual(cfg.target_file, "~/.config/opencode/AGENTS.md")
        self.assertEqual(cfg.host_sources, ("~/AGENTS.md",))
        with self.assertRaises(FrozenInstanceError):
            cfg.target_file = "modified"

    def test_engine_skill_config_immutability(self):
        cfg = EngineSkillConfig(target_dir="~/.config/opencode/skills", catalogs=("skills.json",))
        self.assertEqual(cfg.target_dir, "~/.config/opencode/skills")
        self.assertEqual(cfg.catalogs, ("skills.json",))
        with self.assertRaises(FrozenInstanceError):
            cfg.target_dir = "modified"

    def test_engine_manifest_immutability(self):
        manifest = EngineManifest(
            name="opencode",
            port=4096,
            web_url="http://127.0.0.1:4096",
            rules=EngineRuleConfig(target_file="AGENTS.md"),
            skills=EngineSkillConfig(target_dir="skills"),
            mounts=("~/.config/opencode",),
            directory="/path/to/opencode",
        )
        self.assertEqual(manifest.name, "opencode")
        self.assertEqual(manifest.port, 4096)
        self.assertEqual(manifest.web_url, "http://127.0.0.1:4096")
        self.assertEqual(manifest.mounts, ("~/.config/opencode",))
        with self.assertRaises(FrozenInstanceError):
            manifest.port = 8080

    def test_sandbox_config_defaults(self):
        cfg = SandboxConfig()
        self.assertEqual(cfg.allowed_workspaces, ())
        self.assertEqual(cfg.allowed_commands, {})
        with self.assertRaises(FrozenInstanceError):
            cfg.allowed_workspaces = ("/path",)


if __name__ == "__main__":
    unittest.main()
