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

from agent_sandbox.cli import SandboxCLI, main
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


class TestSelectiveStopRestart(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_cli_selective_")

    def test_stop_single_engine_targets_only_that_service(self):
        cli = _cli_with_engines(self.tmpdir)
        both = (cli.registry.get("opencode"), cli.registry.get("antigravity"))
        with (
            mock.patch.object(cli, "get_running_engines", return_value=both),
            mock.patch.object(cli, "run_compose", return_value=0) as rc,
            mock.patch.object(cli, "stop_host_bridge") as sb,
        ):
            cli.cmd_stop(["opencode"])
        self.assertEqual(rc.call_args[0][0], ["stop", "opencode"])
        self.assertFalse(sb.called)

    def test_stop_last_engine_stops_bridge(self):
        cli = _cli_with_engines(self.tmpdir)
        only_opencode = (cli.registry.get("opencode"),)
        with (
            mock.patch.object(cli, "get_running_engines", return_value=only_opencode),
            mock.patch.object(cli, "run_compose", return_value=0) as rc,
            mock.patch.object(cli, "stop_host_bridge") as sb,
        ):
            cli.cmd_stop(["opencode"])
        self.assertEqual(rc.call_args[0][0], ["stop", "opencode"])
        self.assertTrue(sb.called)

    def test_stop_failure_keeps_bridge(self):
        cli = _cli_with_engines(self.tmpdir)
        only_opencode = (cli.registry.get("opencode"),)
        with (
            mock.patch.object(cli, "get_running_engines", return_value=only_opencode),
            mock.patch.object(cli, "run_compose", return_value=1) as rc,
            mock.patch.object(cli, "stop_host_bridge") as sb,
        ):
            code = cli.cmd_stop(["opencode"])
        self.assertEqual(code, 1)
        self.assertFalse(sb.called)

    def test_stop_all_downs_and_stops_bridge(self):
        cli = _cli_with_engines(self.tmpdir)
        with mock.patch.object(cli, "run_compose", return_value=0) as rc, mock.patch.object(
            cli, "stop_host_bridge"
        ) as sb:
            cli.cmd_stop([])
        self.assertEqual(rc.call_args[0][0], ["down"])
        self.assertTrue(sb.called)

    def test_restart_without_engines_does_not_down_everything(self):
        cli = _cli_with_engines(self.tmpdir)
        with mock.patch.object(cli, "run_compose", return_value=0) as rc:
            code = cli.cmd_restart([])
        self.assertEqual(code, 1)
        self.assertFalse(rc.called)

    def test_restart_single_engine_stops_and_starts_only_it(self):
        cli = _cli_with_engines(self.tmpdir)
        both = (cli.registry.get("opencode"), cli.registry.get("antigravity"))
        with (
            mock.patch.object(cli, "get_running_engines", return_value=both),
            mock.patch.object(cli, "ensure_scaffolding"),
            mock.patch.object(cli, "run_legacy_engine_migrations"),
            mock.patch("agent_sandbox.cli.load_all_runtimes", return_value=()),
            mock.patch("agent_sandbox.cli.runtimes_for_active", return_value=()),
            mock.patch("agent_sandbox.cli.compile_rules_for_engine"),
            mock.patch("agent_sandbox.cli.generate_compose_override") as gen,
            mock.patch.object(cli, "start_host_bridge"),
            mock.patch.object(cli, "stop_host_bridge") as sb,
            mock.patch.object(cli, "run_compose", return_value=0) as rc,
        ):
            cli.cmd_restart(["opencode"])
        self.assertEqual(rc.call_args_list[0][0][0], ["stop", "opencode"])
        self.assertEqual(rc.call_args_list[1][0][0], ["up", "-d", "opencode"])
        names = {e.name for e in gen.call_args[1]["active_engines"]}
        self.assertEqual(names, {"opencode"})
        self.assertFalse(sb.called)

    def test_start_targets_only_requested_engine(self):
        cli = _cli_with_engines(self.tmpdir)
        with (
            mock.patch.object(cli, "ensure_scaffolding"),
            mock.patch.object(cli, "run_legacy_engine_migrations"),
            mock.patch("agent_sandbox.cli.load_all_runtimes", return_value=()),
            mock.patch("agent_sandbox.cli.runtimes_for_active", return_value=()),
            mock.patch("agent_sandbox.cli.compile_rules_for_engine"),
            mock.patch("agent_sandbox.cli.generate_compose_override") as gen,
            mock.patch.object(cli, "start_host_bridge"),
            mock.patch.object(cli, "run_compose", return_value=0) as rc,
        ):
            cli.cmd_start(["opencode"])
        self.assertEqual(rc.call_args[0][0], ["up", "-d", "opencode"])
        names = {e.name for e in gen.call_args[1]["active_engines"]}
        self.assertEqual(names, {"opencode"})


class TestHostBridgeCLI(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_cli_hb_")
        self.env_patcher = mock.patch.dict(os.environ, {"AGENT_SANDBOX_REPO_ROOT": self.tmpdir})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_is_host_bridge_port_open_false_on_refused(self):
        cli = _cli_with_engines(self.tmpdir)
        with mock.patch("socket.socket") as mock_sock:
            inst = mock.MagicMock()
            inst.connect.side_effect = ConnectionRefusedError
            mock_sock.return_value.__enter__.return_value = inst
            self.assertFalse(cli.is_host_bridge_port_open())

    def test_is_host_bridge_port_open_true_on_success(self):
        cli = _cli_with_engines(self.tmpdir)
        with mock.patch("socket.socket") as mock_sock:
            inst = mock.MagicMock()
            inst.connect.return_value = None
            mock_sock.return_value.__enter__.return_value = inst
            self.assertTrue(cli.is_host_bridge_port_open())

    def test_main_host_bridge_fg_refuses_when_daemon_running(self):
        with (
            mock.patch.object(SandboxCLI, "get_host_bridge_pid", return_value=12345),
            mock.patch("subprocess.run") as mock_run,
        ):
            code = main(["host-bridge", "fg"])
        self.assertEqual(code, 1)
        self.assertFalse(mock_run.called)

    def test_main_host_bridge_fg_refuses_when_port_in_use(self):
        with (
            mock.patch.object(SandboxCLI, "get_host_bridge_pid", return_value=None),
            mock.patch.object(SandboxCLI, "is_host_bridge_port_open", return_value=True),
            mock.patch("subprocess.run") as mock_run,
        ):
            code = main(["host-bridge", "fg"])
        self.assertEqual(code, 1)
        self.assertFalse(mock_run.called)


if __name__ == "__main__":
    unittest.main()
