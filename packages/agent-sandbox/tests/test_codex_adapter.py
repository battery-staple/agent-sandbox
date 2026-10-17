"""Tests for Codex engine adapter: manifest, config parsing, and preflight warnings."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))

from agent_sandbox.config.loader import load_engine_manifest
from agent_sandbox.engines import get_adapter
from agent_sandbox.engines.codex import (
    CodexAdapter,
    CodexUserConfig,
    parse_codex_user_config,
)


def _fake_run(container_stdout="", container_rc=0, host_stdout="", host_rc=0):
    """Returns a subprocess.run stand-in distinguishing docker from the host binary."""

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "docker":
            return SimpleNamespace(returncode=container_rc, stdout=container_stdout, stderr="")
        return SimpleNamespace(returncode=host_rc, stdout=host_stdout, stderr="")

    return fake_run


def _fake_app(tmpdir: str) -> str:
    """Creates a fake desktop app bundle containing a codex binary; returns its path."""
    app = Path(tmpdir) / "ChatGPT.app"
    binary = app / "Contents" / "Resources" / "codex"
    binary.parent.mkdir(parents=True)
    binary.touch()
    return str(app)


class TestCodexManifest(unittest.TestCase):
    def test_manifest_loads_and_validates(self):
        manifest_path = os.path.join(REPO_ROOT, "engines", "codex", "manifest.yaml")
        m = load_engine_manifest(manifest_path, expected_dir_name="codex")
        self.assertEqual(m.name, "codex")
        self.assertEqual(m.port, 58434)
        self.assertEqual(m.connection.type, "ssh")
        self.assertEqual(m.connection.user, "developer")
        self.assertEqual(m.connection.host_alias, "agent-sandbox-codex")
        self.assertEqual(m.rules.target_file, "~/.codex/AGENTS.md")
        self.assertEqual(m.rules.host_sources, ("~/.codex/AGENTS.md",))
        self.assertEqual(m.skills.target_dir, "~/.agents/skills")
        self.assertEqual(m.mounts, ())


class TestCodexUserConfig(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("codex")

    def test_missing_file_returns_default(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = parse_codex_user_config(Path(d) / "config.yaml")
            self.assertEqual(cfg, CodexUserConfig())
            self.assertEqual(self.adapter.default_config(), CodexUserConfig())

    def test_empty_and_null_files_return_default(self):
        with tempfile.TemporaryDirectory() as d:
            for content in ("", "null\n", "{}\n"):
                p = Path(d) / "config.yaml"
                p.write_text(content, encoding="utf-8")
                self.assertEqual(parse_codex_user_config(p), CodexUserConfig())

    def test_unexpected_keys_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("api_key: secret\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_codex_user_config(p)
            self.assertIn(f"Invalid codex config {p}", str(ctx.exception))
            self.assertIn("unexpected keys: ['api_key']", str(ctx.exception))

            p.write_text("a: 1\nb: 2\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                self.adapter.parse_user_config(p)
            self.assertIn("unexpected keys: ['a', 'b']", str(ctx.exception))

    def test_non_mapping_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text("- one\n- two\n", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                parse_codex_user_config(p)
            self.assertIn("top level must be a mapping", str(ctx.exception))

    def test_scaffold_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            sandbox_dir = Path(d)
            result = self.adapter.scaffold(sandbox_dir, Path(REPO_ROOT))
            self.assertIsNone(result)
            self.assertFalse((sandbox_dir / "codex" / "config.yaml").exists())


class TestCodexAdapterHooks(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("codex")

    def test_compose_env_empty(self):
        self.assertEqual(self.adapter.compose_env(CodexUserConfig()), ())

    def test_compose_env_type_check(self):
        with self.assertRaises(TypeError) as ctx:
            self.adapter.compose_env("not a config")
        self.assertIn("CodexAdapter.compose_env expects CodexUserConfig", str(ctx.exception))

    def test_status_lines(self):
        lines = self.adapter.status_lines(CodexUserConfig())
        self.assertEqual(len(lines), 2)
        self.assertIn("alias agent-sandbox-codex", lines[0])
        self.assertIn("codex login --device-auth", lines[1])

    def test_status_lines_type_check(self):
        with self.assertRaises(TypeError) as ctx:
            self.adapter.status_lines("not a config")
        self.assertIn("CodexAdapter.status_lines expects CodexUserConfig", str(ctx.exception))


class TestCodexPreflightWarnings(unittest.TestCase):
    def setUp(self):
        self.adapter = get_adapter("codex")
        self.tmpdir = tempfile.mkdtemp(prefix="test_codex_preflight_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_version_mismatch_returns_warning_naming_both(self):
        app = _fake_app(self.tmpdir)
        with mock.patch("agent_sandbox.engines.codex.HOST_APP_CANDIDATES", (app,)), \
                mock.patch(
                    "agent_sandbox.engines.codex.subprocess.run",
                    side_effect=_fake_run(
                        container_stdout="codex-cli 0.162.1\n",
                        host_stdout="codex-cli 0.163.0\n",
                    ),
                ):
            warnings = self.adapter.preflight_warnings(Path(self.tmpdir))
        self.assertEqual(len(warnings), 1)
        self.assertIn("0.162.1", warnings[0])
        self.assertIn("0.163.0", warnings[0])
        self.assertIn("desktop app may have updated", warnings[0])

    def test_version_match_returns_no_warnings(self):
        app = _fake_app(self.tmpdir)
        with mock.patch("agent_sandbox.engines.codex.HOST_APP_CANDIDATES", (app,)), \
                mock.patch(
                    "agent_sandbox.engines.codex.subprocess.run",
                    side_effect=_fake_run(
                        container_stdout="codex-cli 0.162.1\n",
                        host_stdout="codex-cli 0.162.1\n",
                    ),
                ):
            self.assertEqual(self.adapter.preflight_warnings(Path(self.tmpdir)), ())

    def test_missing_host_binary_returns_no_warnings(self):
        missing_app = os.path.join(self.tmpdir, "Missing.app")
        with mock.patch("agent_sandbox.engines.codex.HOST_APP_CANDIDATES", (missing_app,)), \
                mock.patch(
                    "agent_sandbox.engines.codex.subprocess.run",
                    side_effect=_fake_run(container_stdout="codex-cli 0.162.1\n"),
                ) as run:
            self.assertEqual(self.adapter.preflight_warnings(Path(self.tmpdir)), ())
        self.assertEqual(run.call_count, 1)  # docker only; no host binary to probe

    def test_docker_exec_failure_returns_no_warnings(self):
        with mock.patch(
            "agent_sandbox.engines.codex.subprocess.run",
            side_effect=_fake_run(container_stdout="", container_rc=125),
        ) as run:
            self.assertEqual(self.adapter.preflight_warnings(Path(self.tmpdir)), ())
        self.assertEqual(run.call_count, 1)  # short-circuit; no host probe

    def test_subprocess_exception_returns_no_warnings(self):
        with mock.patch(
            "agent_sandbox.engines.codex.subprocess.run",
            side_effect=OSError("docker not found"),
        ):
            self.assertEqual(self.adapter.preflight_warnings(Path(self.tmpdir)), ())

    def test_unparseable_output_returns_no_warnings(self):
        with mock.patch(
            "agent_sandbox.engines.codex.subprocess.run",
            side_effect=_fake_run(container_stdout="codex: version unknown\n"),
        ):
            self.assertEqual(self.adapter.preflight_warnings(Path(self.tmpdir)), ())


class TestCodexRegistration(unittest.TestCase):
    def test_registered_adapter(self):
        adapter = get_adapter("codex")
        self.assertIsInstance(adapter, CodexAdapter)
        self.assertEqual(adapter.name, "codex")

    def test_registered_container_name_resolves(self):
        from agent_sandbox.engines import resolve_engine_name

        self.assertEqual(resolve_engine_name("agent-sandbox-codex"), "codex")


if __name__ == "__main__":
    unittest.main()
