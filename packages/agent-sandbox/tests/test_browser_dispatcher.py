"""
Unit tests for guest/bin/google-chrome browser dispatcher and providers.
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

# Locate guest/bin/google-chrome
REPO_ROOT = Path(__file__).resolve().parents[3]
CHROME_SCRIPT = REPO_ROOT / "guest" / "bin" / "google-chrome"

# Dynamically import guest/bin/google-chrome
loader = importlib.machinery.SourceFileLoader("google_chrome", str(CHROME_SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None and spec.loader is not None
chrome_mod = importlib.util.module_from_spec(spec)
loader.exec_module(chrome_mod)

BrowserProvider = chrome_mod.BrowserProvider
BrowserMode = chrome_mod.BrowserMode
BrowserDispatcher = chrome_mod.BrowserDispatcher
HostBrowserProvider = chrome_mod.HostBrowserProvider
ContainerBrowserProvider = chrome_mod.ContainerBrowserProvider
SocatForwarder = chrome_mod.SocatForwarder
PythonForwarder = chrome_mod.PythonForwarder
create_forwarder = chrome_mod.create_forwarder
extract_browser_flags = chrome_mod.extract_browser_flags
write_devtools_active_port = chrome_mod.write_devtools_active_port
main = chrome_mod.main


class DummyProvider:
    def __init__(self, available: bool = True, exit_code: int = 0) -> None:
        self._available = available
        self.exit_code = exit_code
        self.launched_args: list[str] | None = None

    def is_available(self) -> bool:
        return self._available

    def launch(self, args: list[str]) -> int:
        self.launched_args = list(args)
        return self.exit_code


class TestModeResolution(unittest.TestCase):
    """Test resolution between auto, host, and container modes."""

    def test_auto_mode_prefers_host_when_available(self):
        host = DummyProvider(available=True, exit_code=0)
        container = DummyProvider(available=True, exit_code=0)
        dispatcher = BrowserDispatcher(mode="auto", host_provider=host, container_provider=container)
        self.assertIs(dispatcher.resolve_provider(), host)

    def test_auto_mode_falls_back_to_container_when_host_unavailable(self):
        host = DummyProvider(available=False)
        container = DummyProvider(available=True, exit_code=0)
        dispatcher = BrowserDispatcher(mode="auto", host_provider=host, container_provider=container)
        self.assertIs(dispatcher.resolve_provider(), container)

    def test_auto_mode_fails_when_both_unavailable(self):
        host = DummyProvider(available=False)
        container = DummyProvider(available=False)
        dispatcher = BrowserDispatcher(mode="auto", host_provider=host, container_provider=container)
        with self.assertRaises(RuntimeError) as ctx:
            dispatcher.resolve_provider()
        self.assertIn("No browser provider available in auto mode", str(ctx.exception))

    def test_host_mode_uses_host_when_available(self):
        host = DummyProvider(available=True)
        container = DummyProvider(available=True)
        dispatcher = BrowserDispatcher(mode="host", host_provider=host, container_provider=container)
        self.assertIs(dispatcher.resolve_provider(), host)

    def test_host_mode_fails_when_host_unavailable(self):
        host = DummyProvider(available=False)
        container = DummyProvider(available=True)
        dispatcher = BrowserDispatcher(mode="host", host_provider=host, container_provider=container)
        with self.assertRaises(RuntimeError) as ctx:
            dispatcher.resolve_provider()
        self.assertIn("Host browser requested (BROWSER_MODE=host), but host-exec bridge is unavailable", str(ctx.exception))

    def test_container_mode_uses_container_when_available(self):
        host = DummyProvider(available=True)
        container = DummyProvider(available=True)
        dispatcher = BrowserDispatcher(mode="container", host_provider=host, container_provider=container)
        self.assertIs(dispatcher.resolve_provider(), container)

    def test_container_mode_fails_when_container_unavailable(self):
        host = DummyProvider(available=True)
        container = DummyProvider(available=False)
        dispatcher = BrowserDispatcher(mode="container", host_provider=host, container_provider=container)
        with self.assertRaises(RuntimeError) as ctx:
            dispatcher.resolve_provider()
        self.assertIn("Container browser requested (BROWSER_MODE=container), but Chromium was not found", str(ctx.exception))

    def test_invalid_mode_raises_value_error(self):
        dispatcher = BrowserDispatcher(mode="firefox")
        with self.assertRaises(ValueError) as ctx:
            dispatcher.resolve_provider()
        self.assertIn("Invalid BROWSER_MODE 'firefox'", str(ctx.exception))

    def test_mode_reads_from_environment(self):
        host = DummyProvider(available=True)
        container = DummyProvider(available=True)

        with patch.dict(os.environ, {"BROWSER_MODE": "host"}):
            d = BrowserDispatcher(host_provider=host, container_provider=container)
            self.assertEqual(d.mode, "host")
            self.assertIs(d.resolve_provider(), host)

        with patch.dict(os.environ, {"BROWSER_MODE": "container"}):
            d = BrowserDispatcher(host_provider=host, container_provider=container)
            self.assertEqual(d.mode, "container")
            self.assertIs(d.resolve_provider(), container)

        with patch.dict(os.environ, {"BROWSER_MODE": "auto"}):
            d = BrowserDispatcher(host_provider=host, container_provider=container)
            self.assertEqual(d.mode, "auto")
            self.assertIs(d.resolve_provider(), host)

        with patch.dict(os.environ, {}, clear=True):
            # When unset, defaults to auto
            d = BrowserDispatcher(host_provider=host, container_provider=container)
            self.assertEqual(d.mode, "auto")
            self.assertIs(d.resolve_provider(), host)

    def test_dispatch_forwards_args_to_resolved_provider(self):
        host = DummyProvider(available=True, exit_code=42)
        dispatcher = BrowserDispatcher(mode="host", host_provider=host)
        code = dispatcher.dispatch(["--headless", "https://example.com"])
        self.assertEqual(code, 42)
        self.assertEqual(host.launched_args, ["--headless", "https://example.com"])


class TestContainerBrowserProvider(unittest.TestCase):
    """Test ContainerBrowserProvider argument preparation and binary discovery."""

    def setUp(self):
        self.provider = ContainerBrowserProvider(use_execv=False)

    def test_prepare_args_injects_no_sandbox_when_missing(self):
        cases = [
            ([], ["--no-sandbox"]),
            (["--headless"], ["--no-sandbox", "--headless"]),
            (["--remote-debugging-port=9222", "--headless"], ["--no-sandbox", "--remote-debugging-port=9222", "--headless"]),
        ]
        for input_args, expected in cases:
            self.assertEqual(self.provider.prepare_args(input_args), expected)

    def test_prepare_args_preserves_no_sandbox_when_present(self):
        cases = [
            (["--no-sandbox"], ["--no-sandbox"]),
            (["--no-sandbox", "--headless"], ["--no-sandbox", "--headless"]),
            (["--headless", "--no-sandbox"], ["--headless", "--no-sandbox"]),
            (["--remote-debugging-port=9222", "--no-sandbox", "--headless"], ["--remote-debugging-port=9222", "--no-sandbox", "--headless"]),
        ]
        for input_args, expected in cases:
            self.assertEqual(self.provider.prepare_args(input_args), expected)

    def test_find_chromium_binary_in_playwright_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chrome_dir = Path(tmpdir) / "chromium-1155" / "chrome-linux"
            chrome_dir.mkdir(parents=True)
            chrome_bin = chrome_dir / "chrome"
            chrome_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            chrome_bin.chmod(0o755)

            p = ContainerBrowserProvider(browsers_path=tmpdir, use_execv=False)
            self.assertEqual(p.find_chromium_binary(), str(chrome_bin))
            self.assertTrue(p.is_available())

    def test_find_chromium_binary_system_fallback(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            p = ContainerBrowserProvider(browsers_path=empty_dir, use_execv=False)
            with patch("shutil.which", return_value="/usr/bin/chromium"):
                with patch("os.path.isfile", return_value=True):
                    with patch("os.access", return_value=True):
                        self.assertEqual(p.find_chromium_binary(), "/usr/bin/chromium")
                        self.assertTrue(p.is_available())

    def test_find_chromium_binary_returns_none_when_unavailable(self):
        with tempfile.TemporaryDirectory() as empty_dir:
            p = ContainerBrowserProvider(browsers_path=empty_dir, use_execv=False)
            with patch("shutil.which", return_value=None):
                with patch("os.path.isfile", return_value=False):
                    self.assertIsNone(p.find_chromium_binary())
                    self.assertFalse(p.is_available())

    def test_launch_replaces_via_execv(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chrome_bin = Path(tmpdir) / "chrome"
            chrome_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            chrome_bin.chmod(0o755)

            p = ContainerBrowserProvider(browsers_path=tmpdir, use_execv=True)
            with patch("os.execv") as mock_execv:
                p.launch(["--headless"])
                mock_execv.assert_called_once_with(
                    str(chrome_bin),
                    [str(chrome_bin), "--no-sandbox", "--headless"],
                )

    def test_launch_via_subprocess_when_execv_disabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            chrome_bin = Path(tmpdir) / "chrome"
            chrome_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            chrome_bin.chmod(0o755)

            p = ContainerBrowserProvider(browsers_path=tmpdir, use_execv=False)
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                code = p.launch(["--headless"])
                self.assertEqual(code, 0)
                mock_run.assert_called_once_with([str(chrome_bin), "--no-sandbox", "--headless"])

    def test_launch_fails_when_binary_not_found(self):
        p = ContainerBrowserProvider(browsers_path="/nonexistent/dir", use_execv=False)
        with patch.object(p, "find_chromium_binary", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                p.launch(["--headless"])
            self.assertIn("Chromium binary not found in container", str(ctx.exception))


class TestHostBrowserProvider(unittest.TestCase):
    """Test HostBrowserProvider flag parsing, DevToolsActivePort, and lifecycle."""

    def test_extract_browser_flags_defaults(self):
        port, user_data_dir = extract_browser_flags([])
        self.assertEqual(port, 9222)
        self.assertIsNone(user_data_dir)

        port, user_data_dir = extract_browser_flags(["--headless", "--disable-gpu"])
        self.assertEqual(port, 9222)
        self.assertIsNone(user_data_dir)

    def test_extract_browser_flags_with_values(self):
        # equals syntax
        port, user_data_dir = extract_browser_flags([
            "--remote-debugging-port=9225",
            "--user-data-dir=/tmp/test-profile",
        ])
        self.assertEqual(port, 9225)
        self.assertEqual(user_data_dir, "/tmp/test-profile")

        # space-separated syntax
        port, user_data_dir = extract_browser_flags([
            "--remote-debugging-port", "9226",
            "--user-data-dir", "/tmp/test-profile-2",
        ])
        self.assertEqual(port, 9226)
        self.assertEqual(user_data_dir, "/tmp/test-profile-2")

    def test_extract_browser_flags_zero_port_defaults_to_9222(self):
        port, _ = extract_browser_flags(["--remote-debugging-port=0"])
        self.assertEqual(port, 9222)

    def test_write_devtools_active_port(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "chrome_profile"
            port_file = write_devtools_active_port(str(profile_dir), 9223)

            self.assertTrue(port_file.is_file())
            content = port_file.read_text(encoding="utf-8")
            self.assertEqual(content, "9223\n/devtools/browser/local\n")

    def test_prepare_args_positions_remote_debugging_port_first(self):
        provider = HostBrowserProvider()

        # Missing port in args: prepends default 9222
        prepared = provider.prepare_args(["--headless", "--user-data-dir=/tmp/dir"])
        self.assertEqual(prepared, ["--remote-debugging-port=9222", "--headless", "--user-data-dir=/tmp/dir"])

        # Specified port later in args: moves to front
        prepared = provider.prepare_args(["--headless", "--remote-debugging-port=9227", "--disable-gpu"])
        self.assertEqual(prepared, ["--remote-debugging-port=9227", "--headless", "--disable-gpu"])

        # Space separated port: strips and prepends
        prepared = provider.prepare_args(["--headless", "--remote-debugging-port", "9228"])
        self.assertEqual(prepared, ["--remote-debugging-port=9228", "--headless"])

        # Preserves --version as-is
        prepared = provider.prepare_args(["--version"])
        self.assertEqual(prepared, ["--version"])

    def test_is_available_checks_host_exec_and_daemon(self):
        provider = HostBrowserProvider(daemon_host="127.0.0.1", daemon_port=58433)

        # Missing host-exec binary
        with patch("shutil.which", return_value=None):
            self.assertFalse(provider.is_available())

        # host-exec present but daemon unreachable
        with patch("shutil.which", return_value="/usr/local/bin/host-exec"):
            with patch.object(provider, "check_daemon_reachable", return_value=False):
                self.assertFalse(provider.is_available())

        # host-exec present and daemon reachable
        with patch("shutil.which", return_value="/usr/local/bin/host-exec"):
            with patch.object(provider, "check_daemon_reachable", return_value=True):
                self.assertTrue(provider.is_available())

    def test_launch_lifecycle_and_forwarder_cleanup(self):
        mock_forwarder = MagicMock()
        mock_forwarder_factory = MagicMock(return_value=mock_forwarder)

        with tempfile.TemporaryDirectory() as tmpdir:
            profile_dir = Path(tmpdir) / "profile"
            provider = HostBrowserProvider(
                daemon_host="host.docker.internal",
                daemon_port=58433,
                host_exec_cmd="host-exec",
                forwarder_factory=mock_forwarder_factory,
            )

            # Mock availability
            with patch.object(provider, "is_available", return_value=True):
                mock_proc = MagicMock()
                mock_proc.wait.return_value = 0
                mock_proc.poll.return_value = None

                with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
                    ret = provider.launch([
                        f"--user-data-dir={profile_dir}",
                        "--remote-debugging-port=9230",
                        "--headless",
                    ])
                    self.assertEqual(ret, 0)

                    # 1. DevToolsActivePort was written
                    port_file = profile_dir / "DevToolsActivePort"
                    self.assertTrue(port_file.is_file())
                    self.assertEqual(port_file.read_text(encoding="utf-8"), "9230\n/devtools/browser/local\n")

                    # 2. Forwarder started with local_port=9230, host="host.docker.internal", remote_port=9230
                    mock_forwarder_factory.assert_called_once_with(9230, "host.docker.internal", 9230)
                    mock_forwarder.start.assert_called_once()

                    # 3. host-exec chrome was invoked with --remote-debugging-port=9230 at front
                    mock_popen.assert_called_once_with([
                        "host-exec",
                        "chrome",
                        "--remote-debugging-port=9230",
                        f"--user-data-dir={profile_dir}",
                        "--headless",
                    ])

                    # 4. Forwarder was stopped
                    mock_forwarder.stop.assert_called_once()

    def test_launch_cleans_up_forwarder_on_exception(self):
        mock_forwarder = MagicMock()
        mock_forwarder_factory = MagicMock(return_value=mock_forwarder)

        provider = HostBrowserProvider(
            daemon_host="host.docker.internal",
            daemon_port=58433,
            forwarder_factory=mock_forwarder_factory,
        )

        with patch.object(provider, "is_available", return_value=True):
            with patch("subprocess.Popen", side_effect=OSError("Command failed")):
                with self.assertRaises(OSError):
                    provider.launch(["--remote-debugging-port=9231"])

                # Forwarder started and cleaned up even on exception
                mock_forwarder.start.assert_called_once()
                mock_forwarder.stop.assert_called_once()

    def test_launch_version_does_not_start_forwarder(self):
        mock_forwarder = MagicMock()
        mock_forwarder_factory = MagicMock(return_value=mock_forwarder)

        provider = HostBrowserProvider(
            daemon_host="host.docker.internal",
            daemon_port=58433,
            forwarder_factory=mock_forwarder_factory,
        )

        with patch.object(provider, "is_available", return_value=True):
            mock_proc = MagicMock()
            mock_proc.wait.return_value = 0
            with patch("subprocess.Popen", return_value=mock_proc):
                ret = provider.launch(["--version"])
                self.assertEqual(ret, 0)
                mock_forwarder_factory.assert_not_called()
                mock_forwarder.start.assert_not_called()

    def test_launch_fails_when_host_unavailable(self):
        provider = HostBrowserProvider(daemon_host="host.docker.internal", daemon_port=58433)
        with patch.object(provider, "is_available", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                provider.launch(["--headless"])
            self.assertIn("HostBrowserProvider is unavailable", str(ctx.exception))


class TestForwarders(unittest.TestCase):
    """Test SocatForwarder and PythonForwarder functionality."""

    def test_socat_forwarder_lifecycle(self):
        forwarder = SocatForwarder(9222, "host.docker.internal", 9222)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None

        with patch("subprocess.Popen", return_value=mock_proc) as mock_popen:
            forwarder.start()
            mock_popen.assert_called_once_with(
                [
                    "socat",
                    "TCP-LISTEN:9222,fork,bind=127.0.0.1,reuseaddr",
                    "TCP:host.docker.internal:9222",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            forwarder.stop()
            mock_proc.terminate.assert_called_once()
            mock_proc.wait.assert_called_once()

    def test_python_forwarder_tcp_traffic(self):
        """Test PythonForwarder proxies actual TCP bytes between endpoints."""
        # 1. Start a dummy echo server
        echo_server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        echo_server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        echo_server.bind(("127.0.0.1", 0))
        echo_server.listen(1)
        remote_port = echo_server.getsockname()[1]

        def echo_worker():
            try:
                conn, _ = echo_server.accept()
                data = conn.recv(1024)
                conn.sendall(b"ECHO:" + data)
                conn.close()
            except Exception:
                pass
            finally:
                echo_server.close()

        echo_thread = threading.Thread(target=echo_worker, daemon=True)
        echo_thread.start()

        # 2. Pick a free local port for forwarder
        temp_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        temp_sock.bind(("127.0.0.1", 0))
        local_port = temp_sock.getsockname()[1]
        temp_sock.close()

        # 3. Start Python forwarder
        forwarder = PythonForwarder(local_port, "127.0.0.1", remote_port)
        forwarder.start()
        time.sleep(0.1)

        try:
            # 4. Connect client to forwarder and send payload
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            client.connect(("127.0.0.1", local_port))
            client.sendall(b"hello forwarder")
            response = client.recv(1024)
            client.close()

            self.assertEqual(response, b"ECHO:hello forwarder")
        finally:
            forwarder.stop()
            echo_thread.join(timeout=1.0)


class TestMainDispatcherCLI(unittest.TestCase):
    """Test CLI main() entry point."""

    def test_main_success(self):
        host = DummyProvider(available=True, exit_code=0)
        with patch.object(chrome_mod, "BrowserDispatcher") as mock_dispatcher_cls:
            mock_dispatcher = MagicMock()
            mock_dispatcher.dispatch.return_value = 0
            mock_dispatcher_cls.return_value = mock_dispatcher

            code = main(["--version"])
            self.assertEqual(code, 0)
            mock_dispatcher.dispatch.assert_called_once_with(["--version"])

    def test_main_error_handling(self):
        with patch.object(chrome_mod, "BrowserDispatcher") as mock_dispatcher_cls:
            mock_dispatcher = MagicMock()
            mock_dispatcher.dispatch.side_effect = RuntimeError("Bridge unavailable")
            mock_dispatcher_cls.return_value = mock_dispatcher

            with patch("sys.stderr.write"):
                code = main(["--headless"])
                self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
