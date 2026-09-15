"""Tests for SandboxCLI.get_running_engines returning resolved manifests."""
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.cli import SandboxCLI
from agent_sandbox.config.models import EngineManifest


def _cli_with_engines(tmpdir: str) -> SandboxCLI:
    cli = SandboxCLI(repo_root=tmpdir, sandbox_dir=os.path.join(tmpdir, "sandbox"))
    cli.registry.register(EngineManifest(name="opencode", port=4096, web_url="http://127.0.0.1:4096"))
    cli.registry.register(EngineManifest(name="antigravity", port=58432, web_url="https://localhost:58432"))
    return cli


class TestGetRunningEngines(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_cli_running_")

    def test_resolves_canonical_legacy_and_ignores_unrelated(self):
        cli = _cli_with_engines(self.tmpdir)
        proc = SimpleNamespace(
            returncode=0,
            stdout="agent-sandbox-opencode\nantigravity-sandbox\npostgres\n",
        )
        with mock.patch("subprocess.run", return_value=proc):
            running = cli.get_running_engines()
        self.assertEqual([m.name for m in running], ["opencode", "antigravity"])
        self.assertTrue(all(isinstance(m, EngineManifest) for m in running))

    def test_unknown_prefixed_container_raises(self):
        cli = _cli_with_engines(self.tmpdir)
        proc = SimpleNamespace(returncode=0, stdout="agent-sandbox-ghost\n")
        with mock.patch("subprocess.run", return_value=proc):
            with self.assertRaises(KeyError):
                cli.get_running_engines()

    def test_docker_failure_degrades_to_empty(self):
        cli = _cli_with_engines(self.tmpdir)
        with mock.patch("subprocess.run", side_effect=OSError("no docker")):
            self.assertEqual(cli.get_running_engines(), ())
        proc = SimpleNamespace(returncode=1, stdout="")
        with mock.patch("subprocess.run", return_value=proc):
            self.assertEqual(cli.get_running_engines(), ())


if __name__ == "__main__":
    unittest.main()
