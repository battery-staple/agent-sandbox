"""Tests for SSH-connection CLI behavior: ui, start/status output, and shell."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.cli import SandboxCLI, app, ssh_endpoint
from agent_sandbox.config.models import EngineConnectionConfig, EngineManifest, SandboxConfig
from agent_sandbox.engines import EngineRuntime
from agent_sandbox.engines.opencode import OpencodeAdapter
from agent_sandbox.ssh import (
    ensure_ssh_client_config as _real_ensure_ssh_client_config,
    ensure_ssh_keypair as _real_ensure_ssh_keypair,
)

SSH_MANIFEST = EngineManifest(
    name="fake",
    port=1234,
    connection=EngineConnectionConfig(
        type="ssh", user="developer", host_alias="agent-sandbox-fake"
    ),
)
WEB_MANIFEST = EngineManifest(
    name="opencode",
    port=4096,
    connection=EngineConnectionConfig(type="web", url="http://127.0.0.1:4096"),
)
DEEP_LINK = "codex://settings/connections/ssh/add?name=agent-sandbox-fake"


def _cli_with_engines(tmpdir: str) -> SandboxCLI:
    # Root the CLI at a temp "home" so ssh-config writes never touch the real
    # ~/.ssh/config even if a test forgets to patch the helper.
    fs_root = Path(tmpdir) / "home"
    fs_root.mkdir(parents=True, exist_ok=True)
    cli = SandboxCLI(
        repo_root=tmpdir,
        sandbox_dir=os.path.join(tmpdir, "sandbox"),
        fs_root=fs_root,
    )
    cli.registry.register(SSH_MANIFEST)
    cli.registry.register(WEB_MANIFEST)
    return cli


def _runtime(manifest: EngineManifest) -> EngineRuntime:
    adapter = OpencodeAdapter()
    return EngineRuntime(manifest=manifest, adapter=adapter, config=adapter.default_config())


class TestUiSshBranch(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_cli_ssh_ui_")
        self.cli = _cli_with_engines(self.tmpdir)
        self.fs_root = self.cli.fs_root
        self.user_ssh_config = self.fs_root / ".ssh" / "config"
        # Pre-create the keypair so the real ensure_ssh_client_config (which
        # the ui ssh branch runs below) never needs ssh-keygen while
        # subprocess.run is patched.
        _real_ensure_ssh_keypair(SSH_MANIFEST, self.cli.sandbox_dir)
        self.out = io.StringIO()
        self.err = io.StringIO()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _run_ui(self, which_result=None, platform="darwin", app_exists=True, run_result=None):
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(self.cli, "ensure_scaffolding", return_value=SandboxConfig()),
            mock.patch(
                "agent_sandbox.cli.load_all_runtimes", return_value=(_runtime(SSH_MANIFEST),)
            ),
            mock.patch.object(self.cli, "get_running_engines", return_value=(SSH_MANIFEST,)),
            # wraps: records the call AND runs the real implementation, which
            # must write its marker block into <fs_root>/.ssh/config.
            mock.patch(
                "agent_sandbox.cli.ensure_ssh_client_config",
                wraps=_real_ensure_ssh_client_config,
            ) as mock_cfg,
            mock.patch("agent_sandbox.cli.shutil.which", return_value=which_result) as mock_which,
            mock.patch("agent_sandbox.cli.sys.platform", platform),
            mock.patch("os.path.isdir", return_value=app_exists),
            mock.patch(
                "subprocess.run",
                return_value=run_result or SimpleNamespace(returncode=0),
            ) as mock_run,
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = self.cli.cmd_ui("fake")
        return code, out.getvalue(), err.getvalue(), mock_cfg, mock_which, mock_run

    def test_ui_ssh_invokes_ssh_branch(self):
        code, out, err, mock_cfg, mock_which, mock_run = self._run_ui(platform="darwin")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        mock_cfg.assert_called_once_with(
            SSH_MANIFEST, self.cli.sandbox_dir, fs_root=self.cli.fs_root
        )
        # The literal Host block lands at the top of <fs_root>/.ssh/config, so
        # the app's host picker sees the alias and first-match-wins keeps it.
        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# >>> agent-sandbox:fake >>>\n"))
        self.assertIn("Host agent-sandbox-fake\n", content)
        self.assertEqual(content.count("# >>> agent-sandbox:fake >>>"), 1)
        self.assertIn(
            f"SSH client config ready: {self.fs_root / '.ssh' / 'config'}", out
        )
        # App launched, then the deep link opened. codex not on PATH -> feature skipped.
        self.assertEqual(
            [c[0][0] for c in mock_run.call_args_list],
            [["open", "-a", "ChatGPT"], ["open", DEEP_LINK]],
        )
        self.assertFalse(
            any(c[0][0][0] == "codex" for c in mock_run.call_args_list),
            "codex feature enable must be skipped when codex is not on PATH",
        )
        self.assertIn("Choose your workspace folder in the app to finish.", out)

    def test_ui_ssh_enables_codex_feature_when_on_path(self):
        code, out, err, mock_cfg, mock_which, mock_run = self._run_ui(
            which_result="/usr/local/bin/codex", platform="darwin"
        )
        self.assertEqual(code, 0)
        self.assertIn(
            ["codex", "features", "enable", "remote_connections"],
            [c[0][0] for c in mock_run.call_args_list],
        )

    def test_ui_ssh_non_darwin_prints_deep_link_instruction(self):
        code, out, err, mock_cfg, mock_which, mock_run = self._run_ui(platform="linux")
        self.assertEqual(code, 0)
        mock_run.assert_not_called()
        self.assertIn(f"Open this link in the desktop app: {DEEP_LINK}", out)
        self.assertIn("Choose your workspace folder in the app to finish.", out)

    def test_ui_ssh_app_launch_failure_prints_guidance(self):
        code, out, err, mock_cfg, mock_which, mock_run = self._run_ui(
            platform="darwin", run_result=SimpleNamespace(returncode=1)
        )
        self.assertEqual(code, 0)
        self.assertIn("Could not launch the 'ChatGPT' desktop app", out)
        self.assertIn(DEEP_LINK, out)
        # Only the app-launch attempt; no deep link opened after the failure.
        self.assertEqual([c[0][0] for c in mock_run.call_args_list], [["open", "-a", "ChatGPT"]])

    def test_ui_ssh_config_failure_returns_error(self):
        with (
            mock.patch.object(self.cli, "ensure_scaffolding", return_value=SandboxConfig()),
            mock.patch(
                "agent_sandbox.cli.load_all_runtimes", return_value=(_runtime(SSH_MANIFEST),)
            ),
            mock.patch.object(self.cli, "get_running_engines", return_value=(SSH_MANIFEST,)),
            mock.patch(
                "agent_sandbox.cli.ensure_ssh_client_config",
                side_effect=RuntimeError("ssh-keygen not found"),
            ),
            mock.patch("subprocess.run") as mock_run,
            redirect_stdout(self.out),
            redirect_stderr(self.err),
        ):
            code = self.cli.cmd_ui("fake")
        self.assertEqual(code, 1)
        self.assertIn("ssh-keygen not found", self.err.getvalue())
        mock_run.assert_not_called()


class TestStartStatusSshOutput(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_cli_ssh_status_")
        self.cli = _cli_with_engines(self.tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_status_prints_ssh_endpoint_for_ssh_and_url_for_web(self):
        out = io.StringIO()
        with (
            mock.patch.object(self.cli, "ensure_scaffolding", return_value=SandboxConfig()),
            mock.patch(
                "agent_sandbox.cli.load_all_runtimes",
                return_value=(_runtime(SSH_MANIFEST), _runtime(WEB_MANIFEST)),
            ),
            mock.patch.object(self.cli, "get_running_engines", return_value=(SSH_MANIFEST,)),
            mock.patch.object(self.cli, "get_host_bridge_pid", return_value=None),
            mock.patch.object(self.cli, "is_host_bridge_port_open", return_value=False),
            redirect_stdout(out),
        ):
            code = self.cli.cmd_status()
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("- fake", text)
        self.assertIn(
            "SSH: developer@127.0.0.1:1234 (alias agent-sandbox-fake)", text
        )
        self.assertIn("- opencode", text)
        self.assertIn("URL: http://127.0.0.1:4096", text)
        self.assertNotIn("URL: None", text)

    def test_start_prints_ssh_endpoint_and_preflight_warnings(self):
        preflight = mock.Mock(return_value=("codex version mismatch",))
        adapter = SimpleNamespace(preflight_warnings=preflight, status_lines=lambda cfg: ())
        runtime = EngineRuntime(manifest=SSH_MANIFEST, adapter=adapter, config=None)
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(self.cli, "ensure_scaffolding", return_value=SandboxConfig()),
            mock.patch.object(self.cli, "run_legacy_engine_migrations"),
            mock.patch("agent_sandbox.cli.load_all_runtimes", return_value=(runtime,)),
            mock.patch("agent_sandbox.cli.runtimes_for_active", return_value=(runtime,)),
            mock.patch("agent_sandbox.cli.compile_rules_for_engine"),
            mock.patch("agent_sandbox.cli.generate_compose_override"),
            mock.patch.object(self.cli, "start_host_bridge"),
            mock.patch.object(self.cli, "run_compose", return_value=0),
            redirect_stdout(out),
            redirect_stderr(err),
        ):
            code = self.cli.cmd_start(["fake"])
        self.assertEqual(code, 0)
        self.assertIn(
            "- [FAKE] SSH: developer@127.0.0.1:1234 (alias agent-sandbox-fake)",
            out.getvalue(),
        )
        self.assertIn("[Sandbox Warning] codex version mismatch", err.getvalue())
        preflight.assert_called_once_with(Path(self.cli.sandbox_dir))

    def test_ssh_endpoint_helper_format(self):
        self.assertEqual(
            ssh_endpoint(SSH_MANIFEST),
            "SSH: developer@127.0.0.1:1234 (alias agent-sandbox-fake)",
        )


class TestShellCommand(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_cli_ssh_shell_")
        self.cli = _cli_with_engines(self.tmpdir)
        self.err = io.StringIO()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_shell_default_argv_is_interactive_login_shell(self):
        with (
            mock.patch.object(self.cli, "get_running_engines", return_value=(SSH_MANIFEST,)),
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0)) as mock_run,
        ):
            code = self.cli.cmd_shell("fake")
        self.assertEqual(code, 0)
        mock_run.assert_called_once_with(
            ["docker", "exec", "-it", "--user", "developer", "agent-sandbox-fake", "bash", "-l"],
            check=False,
        )

    def test_shell_dispatch_passes_command_after_separator(self):
        with (
            mock.patch.object(self.cli, "get_running_engines", return_value=(SSH_MANIFEST,)),
            mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=7)) as mock_run,
        ):
            code = app.dispatch(self.cli, ["shell", "fake", "--", "ls", "-la"])
        self.assertEqual(code, 7)
        mock_run.assert_called_once_with(
            ["docker", "exec", "-it", "--user", "developer", "agent-sandbox-fake", "ls", "-la"],
            check=False,
        )

    def test_shell_unknown_engine_returns_error(self):
        with (
            mock.patch("subprocess.run") as mock_run,
            redirect_stderr(self.err),
        ):
            code = self.cli.cmd_shell("nonexistent")
        self.assertEqual(code, 1)
        mock_run.assert_not_called()
        self.assertIn("Unknown engine 'nonexistent'", self.err.getvalue())

    def test_shell_not_running_returns_error(self):
        with (
            mock.patch.object(self.cli, "get_running_engines", return_value=()),
            mock.patch("subprocess.run") as mock_run,
            redirect_stderr(self.err),
        ):
            code = self.cli.cmd_shell("fake")
        self.assertEqual(code, 1)
        mock_run.assert_not_called()
        self.assertIn("not running", self.err.getvalue())

    def test_shell_registered_with_custom_usage(self):
        self.assertIn("shell <engine> [command...]", app.format_help())


if __name__ == "__main__":
    unittest.main()
