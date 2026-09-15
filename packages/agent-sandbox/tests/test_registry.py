"""Tests for engine registry in agent_sandbox.registry.engines."""
import os
import sys
import unittest

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agent_sandbox.registry.engines import EngineRegistry, discover_engines
from agent_sandbox.config.models import EngineManifest


class TestRegistry(unittest.TestCase):
    def test_discover_repo_engines(self):
        engines_dir = os.path.join(REPO_ROOT, "engines")
        registry = discover_engines(engines_dir)
        all_engines = registry.list_all()
        names = [e.name for e in all_engines]
        self.assertIn("opencode", names)
        self.assertIn("antigravity", names)

        opencode = registry.get("opencode")
        self.assertEqual(opencode.port, 4096)
        self.assertTrue(registry.has("opencode"))
        self.assertFalse(registry.has("nonexistent_engine"))

    def test_unknown_engine_raises_keyerror(self):
        registry = EngineRegistry()
        with self.assertRaises(KeyError):
            registry.get("foobar")

    def test_duplicate_registration_raises(self):
        registry = EngineRegistry()
        m = EngineManifest(name="test", port=1000, web_url="http://test")
        registry.register(m)
        with self.assertRaises(ValueError):
            registry.register(m)

    def test_resolve_active_engines(self):
        engines_dir = os.path.join(REPO_ROOT, "engines")
        registry = discover_engines(engines_dir)

        # Explicit engines requested
        active = registry.resolve_active(["antigravity", "opencode"])
        self.assertEqual(len(active), 2)
        self.assertEqual([e.name for e in active], ["antigravity", "opencode"])

        # None/empty requested returns empty list
        empty_active = registry.resolve_active(None)
        self.assertEqual(empty_active, [])
        empty_active_2 = registry.resolve_active([])
        self.assertEqual(empty_active_2, [])


if __name__ == "__main__":
    unittest.main()
