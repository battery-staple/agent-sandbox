"""
Unit tests for guest/bin/chrome-devtools-mcp wrapper.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

# Locate guest/bin/chrome-devtools-mcp
REPO_ROOT = Path(__file__).resolve().parents[3]
MCP_SCRIPT = REPO_ROOT / "guest" / "bin" / "chrome-devtools-mcp"

# Dynamically import guest/bin/chrome-devtools-mcp
loader = importlib.machinery.SourceFileLoader("chrome_devtools_mcp", str(MCP_SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None and spec.loader is not None
mcp_mod = importlib.util.module_from_spec(spec)
sys.modules["chrome_devtools_mcp"] = mcp_mod
loader.exec_module(mcp_mod)

BrowserMode = mcp_mod.BrowserMode
Forwarder = mcp_mod.Forwarder
SocatForwarder = mcp_mod.SocatForwarder
PythonForwarder = mcp_mod.PythonForwarder
create_forwarder = mcp_mod.create_forwarder
is_port_listening = mcp_mod.is_port_listening
query_ws_path = mcp_mod.query_ws_path
write_devtools_active_ports = mcp_mod.write_devtools_active_ports
find_host_home = mcp_mod.find_host_home
resolve_host_user_data_dir = mcp_mod.resolve_host_user_data_dir
find_chromium_binary = mcp_mod.find_chromium_binary
find_real_mcp_binary = mcp_mod.find_real_mcp_binary
ChromeMcpWrapper = mcp_mod.ChromeMcpWrapper
main = mcp_mod.main


class DummyForwarder:
    def __init__(self, local_port: int = 9222, remote_host: str = "host.docker.internal", remote_port: int = 9222) -> None:
        self.local_port = local_port
        self.remote_host = remote_host
        self.remote_port = remote_port
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


class TestChromeMcpModeResolution(unittest.TestCase):
    """Test browser mode parsing and validation."""

    def test_default_mode_is_auto(self):
        wrapper = ChromeMcpWrapper()
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(wrapper.mode, "auto")

    def test_explicit_mode_overrides_env(self):
        wrapper = ChromeMcpWrapper(mode="host")
        with patch.dict(os.environ, {"BROWSER_MODE": "container"}):
            self.assertEqual(wrapper.mode, "host")

    def test_env_mode_is_used(self):
        wrapper = ChromeMcpWrapper()
        with patch.dict(os.environ, {"BROWSER_MODE": "container"}):
            self.assertEqual(wrapper.mode, "container")

    def test_invalid_mode_raises(self):
        wrapper = ChromeMcpWrapper(mode="invalid_mode")
        with self.assertRaises(ValueError) as ctx:
            wrapper.setup_browser()
        self.assertIn("Invalid BROWSER_MODE", str(ctx.exception))


class TestDaemonReachability(unittest.TestCase):
    """Test daemon reachability checks."""

    @patch("shutil.which", return_value=None)
    def test_unreachable_when_host_exec_missing(self, mock_which):
        wrapper = ChromeMcpWrapper(daemon_host="127.0.0.1", daemon_port=58433)
        self.assertFalse(wrapper.is_daemon_reachable())

    @patch("shutil.which", return_value="/usr/local/bin/host-exec")
    def test_unreachable_when_port_none(self, mock_which):
        wrapper = ChromeMcpWrapper(daemon_host="127.0.0.1", daemon_port=None)
        wrapper._daemon_port = None
        with patch.dict(os.environ, {"HOST_EXEC_PORT": ""}):
            self.assertFalse(wrapper.is_daemon_reachable())

    @patch("shutil.which", return_value="/usr/local/bin/host-exec")
    @patch("socket.create_connection")
    def test_reachable_when_socket_connects(self, mock_conn, mock_which):
        mock_conn.return_value.__enter__.return_value = MagicMock()
        wrapper = ChromeMcpWrapper(daemon_host="127.0.0.1", daemon_port=58433)
        self.assertTrue(wrapper.is_daemon_reachable())

    @patch("shutil.which", return_value="/usr/local/bin/host-exec")
    @patch("socket.create_connection", side_effect=OSError("Connection refused"))
    def test_unreachable_when_socket_fails(self, mock_conn, mock_which):
        wrapper = ChromeMcpWrapper(daemon_host="127.0.0.1", daemon_port=58433)
        self.assertFalse(wrapper.is_daemon_reachable())


class TestDevToolsActivePortAndWsQuery(unittest.TestCase):
    """Test querying WebSocket endpoint and writing DevToolsActivePort files."""

    @patch("urllib.request.urlopen")
    def test_query_ws_path_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc-123"}'
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        path = query_ws_path("127.0.0.1", 9222)
        self.assertEqual(path, "/devtools/browser/abc-123")

    @patch("urllib.request.urlopen", side_effect=OSError("Network down"))
    def test_query_ws_path_failure_returns_none(self, mock_urlopen):
        path = query_ws_path("127.0.0.1", 9222)
        self.assertIsNone(path)

    def test_write_devtools_active_ports(self):
        with tempfile.TemporaryDirectory() as base_tmp, tempfile.TemporaryDirectory() as d1:
            extra_dirs = [d1]
            with patch("os.path.expanduser", side_effect=lambda p: os.path.join(base_tmp, p.replace("~/", ""))):
                written = write_devtools_active_ports(9222, "/devtools/browser/test-uuid", extra_dirs=extra_dirs)
                self.assertGreaterEqual(len(written), 2)
                f1 = Path(d1) / "DevToolsActivePort"
                self.assertTrue(f1.is_file())
                self.assertEqual(f1.read_text(encoding="utf-8"), "9222\n/devtools/browser/test-uuid\n")


class TestHostUserDataDirResolution(unittest.TestCase):
    """Test resolving host user data directory."""

    def test_env_override(self):
        with patch.dict(os.environ, {"HOST_CHROME_USER_DATA_DIR": "/custom/profile"}):
            self.assertEqual(resolve_host_user_data_dir(), "/custom/profile")

    def test_detected_host_home(self):
        with patch.dict(os.environ, {"HOST_USER_HOME": "/Users/testuser", "HOST_CHROME_USER_DATA_DIR": ""}):
            self.assertEqual(resolve_host_user_data_dir(), "/Users/testuser/.agent-sandbox/host-chrome-profile")


class TestBinaryResolution(unittest.TestCase):
    """Test resolving real MCP binary and Chromium."""

    def test_real_mcp_binary_env_override(self):
        with tempfile.NamedTemporaryFile(suffix=".js") as f:
            with patch.dict(os.environ, {"REAL_CHROME_DEVTOOLS_MCP": f.name}):
                res = find_real_mcp_binary()
                self.assertEqual(res, ["node", f.name])

    def test_find_real_mcp_binary_from_candidate_dirs(self):
        with tempfile.TemporaryDirectory() as tmp_npx:
            mcp_dir = Path(tmp_npx) / "hash1" / "node_modules" / "chrome-devtools-mcp" / "build" / "src" / "bin"
            mcp_dir.mkdir(parents=True)
            target_js = mcp_dir / "chrome-devtools-mcp.js"
            target_js.write_text("console.log('mcp')", encoding="utf-8")

            with patch.dict(os.environ, {"REAL_CHROME_DEVTOOLS_MCP": ""}):
                with patch("os.path.expanduser") as mock_exp:
                    mock_exp.side_effect = lambda p: tmp_npx if "_npx" in p else "/nonexistent"
                    res = find_real_mcp_binary()
                    self.assertEqual(res, ["node", str(target_js)])

    def test_find_real_mcp_binary_fallback(self):
        with patch.dict(os.environ, {"REAL_CHROME_DEVTOOLS_MCP": ""}):
            with patch("os.path.expanduser", return_value="/nonexistent"):
                with patch("pathlib.Path.is_dir", return_value=False):
                    res = find_real_mcp_binary()
                    self.assertTrue(len(res) >= 2)
                    self.assertIn("chrome-devtools-mcp@latest", res)


class TestBrowserSetupHostMode(unittest.TestCase):
    """Test setup_browser in HOST mode."""

    def test_host_mode_fails_when_daemon_unreachable(self):
        wrapper = ChromeMcpWrapper(mode="host")
        wrapper.is_daemon_reachable = MagicMock(return_value=False)
        with self.assertRaises(RuntimeError) as ctx:
            wrapper.setup_browser()
        self.assertIn("Host browser requested", str(ctx.exception))

    @patch("chrome_devtools_mcp.write_devtools_active_ports")
    @patch("chrome_devtools_mcp.query_ws_path")
    @patch("chrome_devtools_mcp.is_port_listening", return_value=False)
    @patch("subprocess.Popen")
    def test_host_mode_spawns_chrome_and_forwarder(self, mock_popen, mock_listening, mock_ws, mock_write):
        dummy_forwarder = DummyForwarder()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc
        mock_ws.side_effect = [None, None, "/devtools/browser/new-guid"]

        wrapper = ChromeMcpWrapper(
            mode="host",
            forwarder_factory=lambda lp, rh, rp: dummy_forwarder,
        )
        wrapper.is_daemon_reachable = MagicMock(return_value=True)

        wrapper.setup_browser(9222)

        # Chrome should have been spawned
        mock_popen.assert_called_once()
        spawned_cmd = mock_popen.call_args[0][0]
        self.assertEqual(spawned_cmd[0], "host-exec")
        self.assertEqual(spawned_cmd[1], "chrome")
        self.assertIn("--remote-debugging-port=9222", spawned_cmd)

        # Forwarder should have started
        self.assertTrue(dummy_forwarder.started)

        # Active port should have been written
        mock_write.assert_called_once_with(9222, "/devtools/browser/new-guid")

        # Cleanup
        wrapper.cleanup()
        self.assertTrue(dummy_forwarder.stopped)
        mock_proc.terminate.assert_called_once()

    @patch("chrome_devtools_mcp.write_devtools_active_ports")
    @patch("chrome_devtools_mcp.query_ws_path", return_value="/devtools/browser/already-running")
    @patch("chrome_devtools_mcp.is_port_listening", return_value=True)
    @patch("subprocess.Popen")
    def test_host_mode_reuses_existing_chrome(self, mock_popen, mock_listening, mock_ws, mock_write):
        dummy_forwarder = DummyForwarder()
        wrapper = ChromeMcpWrapper(
            mode="host",
            forwarder_factory=lambda lp, rh, rp: dummy_forwarder,
        )
        wrapper.is_daemon_reachable = MagicMock(return_value=True)

        wrapper.setup_browser(9222)

        # Chrome should NOT be spawned since it is already alive
        mock_popen.assert_not_called()
        self.assertFalse(dummy_forwarder.started)
        mock_write.assert_called_once_with(9222, "/devtools/browser/already-running")


class TestBrowserSetupContainerMode(unittest.TestCase):
    """Test setup_browser in CONTAINER mode."""

    @patch("chrome_devtools_mcp.find_chromium_binary", return_value=None)
    @patch("chrome_devtools_mcp.is_port_listening", return_value=False)
    def test_container_mode_fails_if_no_chromium(self, mock_listening, mock_find):
        wrapper = ChromeMcpWrapper(mode="container")
        with self.assertRaises(RuntimeError) as ctx:
            wrapper.setup_browser(9222)
        self.assertIn("Chromium binary not found", str(ctx.exception))

    @patch("chrome_devtools_mcp.write_devtools_active_ports")
    @patch("chrome_devtools_mcp.query_ws_path", return_value="/devtools/browser/container-guid")
    @patch("chrome_devtools_mcp.find_chromium_binary", return_value="/usr/bin/chromium")
    @patch("chrome_devtools_mcp.is_port_listening", return_value=False)
    @patch("subprocess.Popen")
    def test_container_mode_spawns_chromium(self, mock_popen, mock_listening, mock_find, mock_ws, mock_write):
        mock_proc = MagicMock()
        mock_popen.return_value = mock_proc

        wrapper = ChromeMcpWrapper(mode="container")
        wrapper.setup_browser(9222)

        mock_popen.assert_called_once()
        spawned_cmd = mock_popen.call_args[0][0]
        self.assertEqual(spawned_cmd[0], "/usr/bin/chromium")
        self.assertIn("--remote-debugging-port=9222", spawned_cmd)
        self.assertIn("--no-sandbox", spawned_cmd)

        mock_write.assert_called_once_with(9222, "/devtools/browser/container-guid")


class TestBrowserSetupAutoMode(unittest.TestCase):
    """Test setup_browser in AUTO mode."""

    @patch.object(ChromeMcpWrapper, "_setup_host_browser")
    @patch.object(ChromeMcpWrapper, "_setup_container_browser")
    def test_auto_prefers_host_when_reachable(self, mock_container, mock_host):
        wrapper = ChromeMcpWrapper(mode="auto")
        wrapper.is_daemon_reachable = MagicMock(return_value=True)

        wrapper.setup_browser(9222)
        mock_host.assert_called_once_with(9222)
        mock_container.assert_not_called()

    @patch.object(ChromeMcpWrapper, "_setup_host_browser")
    @patch.object(ChromeMcpWrapper, "_setup_container_browser")
    def test_auto_falls_back_to_container_when_unreachable(self, mock_container, mock_host):
        wrapper = ChromeMcpWrapper(mode="auto")
        wrapper.is_daemon_reachable = MagicMock(return_value=False)

        wrapper.setup_browser(9222)
        mock_host.assert_not_called()
        mock_container.assert_called_once_with(9222)


class TestFullRunLifecycle(unittest.TestCase):
    """Test run() execution, node delegation, signal handling, and cleanup."""

    @patch.object(ChromeMcpWrapper, "setup_browser")
    @patch("subprocess.Popen")
    def test_run_executes_mcp_and_cleans_up(self, mock_popen, mock_setup):
        mock_node = MagicMock()
        mock_node.wait.return_value = 0
        mock_node.poll.return_value = 0
        mock_popen.return_value = mock_node

        wrapper = ChromeMcpWrapper(
            mode="auto",
            real_mcp_resolver=lambda: ["node", "/path/to/real.js"],
        )
        wrapper.cleanup = MagicMock()

        ret = wrapper.run(["--autoConnect", "--some-flag"])

        self.assertEqual(ret, 0)
        mock_setup.assert_called_once_with(9222)
        mock_popen.assert_called_once_with(
            ["node", "/path/to/real.js", "--autoConnect", "--some-flag"],
            stdin=sys.stdin,
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        wrapper.cleanup.assert_called_once()

    @patch.object(ChromeMcpWrapper, "setup_browser")
    @patch("subprocess.Popen")
    def test_run_forwards_signal_to_child(self, mock_popen, mock_setup):
        mock_node = MagicMock()
        mock_node.poll.return_value = None  # alive
        mock_popen.return_value = mock_node

        wrapper = ChromeMcpWrapper(
            mode="auto",
            real_mcp_resolver=lambda: ["node", "/path/to/real.js"],
        )

        with patch("signal.signal") as mock_signal:
            captured_handler = None

            def record_signal(sig, handler):
                nonlocal captured_handler
                if sig == signal.SIGINT:
                    captured_handler = handler
                return MagicMock()

            mock_signal.side_effect = record_signal

            def simulate_wait():
                if captured_handler:
                    captured_handler(signal.SIGINT, None)
                return 130

            mock_node.wait.side_effect = simulate_wait

            ret = wrapper.run(["--autoConnect"])

            self.assertEqual(ret, 130)
            mock_node.send_signal.assert_called_once_with(signal.SIGINT)


class TestMainCli(unittest.TestCase):
    """Test CLI main() function."""

    @patch.object(ChromeMcpWrapper, "run", return_value=0)
    def test_main_success(self, mock_run):
        ret = main(["--autoConnect"])
        self.assertEqual(ret, 0)
        mock_run.assert_called_once_with(["--autoConnect"])

    @patch.object(ChromeMcpWrapper, "run", side_effect=Exception("Fatal failure"))
    def test_main_error_returns_one(self, mock_run):
        with patch("sys.stderr"):
            ret = main(["--autoConnect"])
            self.assertEqual(ret, 1)


if __name__ == "__main__":
    unittest.main()
