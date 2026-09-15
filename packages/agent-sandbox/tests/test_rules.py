"""Tests for rule compiler in agent_sandbox.compose.rules."""
import os
import shutil
import sys
import tempfile
import unittest

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.compose.rules import compile_rules_for_engine
from agent_sandbox.config.models import EngineManifest, EngineRuleConfig


class TestRules(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_rules_")
        self.repo_root = os.path.join(self.temp_dir, "repo")
        self.sandbox_dir = os.path.join(self.temp_dir, "sandbox")

        # Create built-in rule
        builtin_rules = os.path.join(self.repo_root, "customizations", "rules")
        os.makedirs(builtin_rules)
        with open(os.path.join(builtin_rules, "01-env.md"), "w") as f:
            f.write("Built-in Environment Rule")

        # Create common user rule
        common_rules = os.path.join(self.sandbox_dir, "common", "rules")
        os.makedirs(common_rules)
        with open(os.path.join(common_rules, "10-common.md"), "w") as f:
            f.write("Common User Rule")

        # Create built-in engine rule
        builtin_engine_rules = os.path.join(self.repo_root, "engines", "opencode", "rules")
        os.makedirs(builtin_engine_rules)
        with open(os.path.join(builtin_engine_rules, "15-builtin-engine.md"), "w") as f:
            f.write("Built-in Engine Rule")

        # Create engine-specific rule
        engine_rules = os.path.join(self.sandbox_dir, "opencode", "rules")
        os.makedirs(engine_rules)
        with open(os.path.join(engine_rules, "20-engine.md"), "w") as f:
            f.write("Engine Specific Rule")

        # Create host rule file
        self.host_rule = os.path.join(self.temp_dir, "HOST_AGENTS.md")
        with open(self.host_rule, "w") as f:
            f.write("Host Global Rule")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_rules_compilation_hierarchy(self):
        engine = EngineManifest(
            name="opencode",
            port=4096,
            web_url="http://localhost:4096",
            rules=EngineRuleConfig(
                target_file="~/.config/opencode/AGENTS.md",
                host_sources=(self.host_rule,),
            ),
        )

        res = compile_rules_for_engine(engine, repo_root=self.repo_root, sandbox_dir=self.sandbox_dir)

        self.assertEqual(res.engine_name, "opencode")
        self.assertTrue(os.path.isfile(res.host_compiled_file))
        self.assertTrue(res.host_compiled_file.endswith("AGENTS.md"))

        content = res.content
        self.assertIn("Built-in Environment Rule", content)
        self.assertIn("Common User Rule", content)
        self.assertIn("Built-in Engine Rule", content)
        self.assertIn("Engine Specific Rule", content)
        self.assertIn("Host Global Rule", content)

        # Check order: Built-in before Common before Built-in Engine before User Engine before Host
        pos_builtin = content.find("Built-in Environment Rule")
        pos_common = content.find("Common User Rule")
        pos_builtin_engine = content.find("Built-in Engine Rule")
        pos_engine = content.find("Engine Specific Rule")
        pos_host = content.find("Host Global Rule")

        self.assertTrue(pos_builtin < pos_common < pos_builtin_engine < pos_engine < pos_host)
        self.assertEqual(len(res.sources), 5)


if __name__ == "__main__":
    unittest.main()
