#!/usr/bin/env python3
"""
Unit tests for packages/host-exec-daemon/host_exec_daemon.py
"""

import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

# Add package directory to sys.path
PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PACKAGE_DIR)

from host_exec_daemon import (
    PID_FILE_PATH,
    cleanup,
    format_log_prefix,
    get_command_description,
    get_required_port,
    get_or_create_secret,
    main,
    prompt_user_approval_async,
    resolve_cwd,
    translate_container_path,
    verify_token,
)


class TestResolveCwd(unittest.TestCase):
    # A container home root that does not exist on the test machine, so the
    # translation path is always the one being exercised.
    FAKE_CONTAINER_HOME = "/container-home-for-tests"

    def test_resolve_valid_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(resolve_cwd(tmpdir), tmpdir)

    def test_resolve_nonexistent_directory(self):
        nonexistent = "/path/that/does/not/exist/for/sure_12345"
        self.assertEqual(resolve_cwd(nonexistent), os.path.expanduser("~"))

    def test_resolve_nonexistent_directory_warns_with_both_directories(self):
        # The warning must name the in-container directory and the resolved
        # out-of-container working directory so the fallback is never silent.
        nonexistent = "/path/that/does/not/exist/for/sure_12345"
        with self.assertLogs(level="WARNING") as cm:
            self.assertEqual(resolve_cwd(nonexistent), os.path.expanduser("~"))

        output = "\n".join(cm.output)
        self.assertIn(nonexistent, output)
        self.assertIn(os.path.expanduser("~"), output)

    def test_resolve_valid_directory_does_not_warn(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertNoLogs(level="WARNING"):
                self.assertEqual(resolve_cwd(tmpdir), tmpdir)

    def test_resolve_none_or_empty(self):
        with self.assertNoLogs(level="WARNING"):
            self.assertEqual(resolve_cwd(None), os.path.expanduser("~"))
            self.assertEqual(resolve_cwd(""), os.path.expanduser("~"))

    def test_translates_container_home_path_to_host_home(self):
        # A container-only path (invisible to the host) is mapped from the
        # container home root onto the host home root, keeping the suffix.
        with tempfile.TemporaryDirectory() as host_home:
            expected = os.path.join(host_home, "project", "src")
            os.makedirs(expected)

            self.assertEqual(
                resolve_cwd(
                    f"{self.FAKE_CONTAINER_HOME}/project/src",
                    container_home=self.FAKE_CONTAINER_HOME,
                    host_home=host_home,
                ),
                expected,
            )

    def test_translates_container_home_root_to_host_home(self):
        with tempfile.TemporaryDirectory() as host_home:
            # No warning: the root itself was successfully translated rather
            # than falling back to the home directory.
            with self.assertNoLogs(level="WARNING"):
                self.assertEqual(
                    resolve_cwd(
                        self.FAKE_CONTAINER_HOME,
                        container_home=self.FAKE_CONTAINER_HOME,
                        host_home=host_home,
                    ),
                    host_home,
                )

    def test_translation_normalizes_trailing_slash(self):
        with tempfile.TemporaryDirectory() as host_home:
            expected = os.path.join(host_home, "project")
            os.makedirs(expected)
            self.assertEqual(
                resolve_cwd(
                    f"{self.FAKE_CONTAINER_HOME}/project/",
                    container_home=self.FAKE_CONTAINER_HOME,
                    host_home=host_home,
                ),
                expected,
            )

    def test_translation_does_not_warn(self):
        with tempfile.TemporaryDirectory() as host_home:
            os.makedirs(os.path.join(host_home, "project"))
            with self.assertNoLogs(level="WARNING"):
                resolve_cwd(
                    f"{self.FAKE_CONTAINER_HOME}/project",
                    container_home=self.FAKE_CONTAINER_HOME,
                    host_home=host_home,
                )

    def test_translation_falls_back_to_home_when_host_path_missing(self):
        # When the translated host path does not exist either, keep the old
        # behaviour of running in the host home directory.
        container_cwd = f"{self.FAKE_CONTAINER_HOME}/does-not-exist"
        with tempfile.TemporaryDirectory() as host_home:
            with self.assertLogs(level="WARNING") as cm:
                self.assertEqual(
                    resolve_cwd(
                        container_cwd,
                        container_home=self.FAKE_CONTAINER_HOME,
                        host_home=host_home,
                    ),
                    host_home,
                )
            output = "\n".join(cm.output)
            self.assertIn(container_cwd, output)
            self.assertIn(host_home, output)

    def test_literal_path_wins_over_translation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            container_home = os.path.join(tmpdir, "home", "developer")
            literal = os.path.join(container_home, "project")
            host_home = os.path.join(tmpdir, "host")
            os.makedirs(literal)
            os.makedirs(host_home)

            self.assertEqual(
                resolve_cwd(literal, container_home=container_home, host_home=host_home),
                literal,
            )


class TestTranslateContainerPath(unittest.TestCase):
    def test_translates_nested_path(self):
        self.assertEqual(
            translate_container_path(
                "/home/developer/a/b", container_home="/home/developer", host_home="/host"
            ),
            os.path.join("/host", "a", "b"),
        )

    def test_translates_home_root(self):
        self.assertEqual(
            translate_container_path(
                "/home/developer", container_home="/home/developer", host_home="/host"
            ),
            "/host",
        )

    def test_ignores_sibling_prefix(self):
        # /home/developerfoo must not be treated as living inside /home/developer.
        self.assertIsNone(
            translate_container_path(
                "/home/developerfoo/x", container_home="/home/developer", host_home="/host"
            )
        )

    def test_ignores_paths_outside_container_home(self):
        self.assertIsNone(
            translate_container_path(
                "/Users/me/project", container_home="/home/developer", host_home="/host"
            )
        )

    def test_none_or_empty_returns_none(self):
        self.assertIsNone(translate_container_path(None))
        self.assertIsNone(translate_container_path(""))

    def test_non_string_returns_none(self):
        self.assertIsNone(translate_container_path(123, host_home="/host"))
        self.assertIsNone(translate_container_path(["/home/developer/x"], host_home="/host"))

    def test_normalizes_dotted_segments(self):
        self.assertEqual(
            translate_container_path(
                "/home/developer/./a/../b", container_home="/home/developer", host_home="/host"
            ),
            os.path.join("/host", "b"),
        )

    def test_rejects_parent_traversal_escape(self):
        # A crafted container path must not escape the host home directory.
        self.assertIsNone(
            translate_container_path(
                "/home/developer/../etc/passwd",
                container_home="/home/developer",
                host_home="/host",
            )
        )

    def test_rejects_doubled_slash_escape(self):
        self.assertIsNone(
            translate_container_path(
                "/home/developer//etc", container_home="/home/developer", host_home="/host"
            )
        )

    def test_container_home_root_maps_onto_host_root(self):
        self.assertEqual(
            translate_container_path("/a/b", container_home="/", host_home="/host"),
            os.path.join("/host", "a", "b"),
        )


class TestVerifyToken(unittest.TestCase):
    def setUp(self):
        self.secret = "test-secret-key-1234567890abcdef"

    def _compute_token(self, cmd, args, cwd):
        payload = {"command": cmd, "args": args, "cwd": cwd}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()

    def test_valid_token(self):
        req = {"command": "sw_vers", "args": ["-productVersion"], "cwd": "/tmp"}
        token = self._compute_token("sw_vers", ["-productVersion"], "/tmp")
        self.assertTrue(verify_token(req, token, self.secret))

    def test_extra_metadata_does_not_break_hmac(self):
        # request_id and trajectory_id should not affect HMAC verification
        token = self._compute_token("sw_vers", ["-productVersion"], "/tmp")
        req = {
            "command": "sw_vers",
            "args": ["-productVersion"],
            "cwd": "/tmp",
            "request_id": "req-9988aabb",
            "trajectory_id": "traj-11223344",
        }
        self.assertTrue(verify_token(req, token, self.secret))

    def test_tampered_command_fails(self):
        token = self._compute_token("sw_vers", [], "/tmp")
        req = {"command": "rm", "args": ["-rf", "/"], "cwd": "/tmp"}
        self.assertFalse(verify_token(req, token, self.secret))

    def test_tampered_args_fails(self):
        token = self._compute_token("sw_vers", ["-productVersion"], "/tmp")
        req = {"command": "sw_vers", "args": ["-buildVersion"], "cwd": "/tmp"}
        self.assertFalse(verify_token(req, token, self.secret))

    def test_tampered_cwd_fails(self):
        token = self._compute_token("sw_vers", [], "/tmp")
        req = {"command": "sw_vers", "args": [], "cwd": "/var/root"}
        self.assertFalse(verify_token(req, token, self.secret))

    def test_empty_or_invalid_token(self):
        req = {"command": "sw_vers", "args": [], "cwd": "/tmp"}
        self.assertFalse(verify_token(req, "", self.secret))
        self.assertFalse(verify_token(req, "invalid-token", self.secret))


class TestCommandDescription(unittest.TestCase):
    def test_explicit_description(self):
        policy = {"description": "Custom tool description", "binary_path": "/bin/echo"}
        self.assertEqual(get_command_description("echo", policy), "Custom tool description")

    def test_fallback_description_with_alias(self):
        policy = {"binary_path": "/usr/bin/open", "auto_args_regex": "^-a Simulator$"}
        desc = get_command_description("simulator", policy)
        self.assertIn("Run '/usr/bin/open' via alias 'simulator'", desc)
        self.assertIn("-a Simulator", desc)

    def test_fallback_description_with_dual_regexes(self):
        policy = {
            "binary_path": "/usr/bin/git",
            "auto_args_regex": "^(status|diff)$",
            "approval_args_regex": "^(push|commit)$",
        }
        desc = get_command_description("git", policy)
        self.assertIn("Execute host binary '/usr/bin/git'", desc)
        self.assertIn("(status|diff)", desc)


class TestSecretLifecycle(unittest.TestCase):
    def test_get_or_create_secret_creates_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            orig_state = os.environ.get("AGENT_SANDBOX_STATE_DIR")
            import host_exec_daemon
            orig_ipc_dir = host_exec_daemon.IPC_DIR
            orig_secret_path = host_exec_daemon.AUTH_SECRET_PATH
            try:
                os.environ["AGENT_SANDBOX_STATE_DIR"] = tmpdir
                host_exec_daemon.IPC_DIR = os.path.join(tmpdir, "ipc")
                host_exec_daemon.AUTH_SECRET_PATH = os.path.join(host_exec_daemon.IPC_DIR, "auth_secret.key")
                secret = get_or_create_secret()
                self.assertTrue(len(secret) >= 32)
                # Second call returns same secret
                secret2 = get_or_create_secret()
                self.assertEqual(secret, secret2)
            finally:
                if orig_state:
                    os.environ["AGENT_SANDBOX_STATE_DIR"] = orig_state
                else:
                    os.environ.pop("AGENT_SANDBOX_STATE_DIR", None)
                host_exec_daemon.IPC_DIR = orig_ipc_dir
                host_exec_daemon.AUTH_SECRET_PATH = orig_secret_path


class TestRuntimeConfig(unittest.TestCase):
    def test_port_is_required(self):
        original = os.environ.pop("HOST_EXEC_PORT", None)
        try:
            with self.assertRaisesRegex(RuntimeError, "HOST_EXEC_PORT is not set"):
                get_required_port()
        finally:
            if original is not None:
                os.environ["HOST_EXEC_PORT"] = original

    def test_port_is_validated(self):
        original = os.environ.get("HOST_EXEC_PORT")
        try:
            os.environ["HOST_EXEC_PORT"] = "not-a-port"
            with self.assertRaisesRegex(RuntimeError, "must be an integer"):
                get_required_port()
        finally:
            if original is None:
                os.environ.pop("HOST_EXEC_PORT", None)
            else:
                os.environ["HOST_EXEC_PORT"] = original


class TestFormatLogPrefix(unittest.TestCase):
    def test_format_prefix(self):
        prefix = format_log_prefix("req-12345678", "traj-abcdefgh", "sw_vers")
        self.assertIn("req-123456", prefix)
        self.assertIn("traj-abc", prefix)
        self.assertIn("sw_vers", prefix)

    def test_format_prefix_empty(self):
        prefix = format_log_prefix("", "")
        self.assertIn("req-init", prefix)
        self.assertIn("manual", prefix)


class TestPromptUserApprovalAsync(unittest.IsolatedAsyncioTestCase):
    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_cocoa_approval_success(self, mock_exec):
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"Approve\n", b"")
        proc_mock.returncode = 0
        mock_exec.return_value = proc_mock

        res = await prompt_user_approval_async(
            "git",
            ["push", "origin", "main"],
            req_id="req-123",
            traj_id="traj-456",
            cwd="/Users/example/project",
        )
        self.assertTrue(res)

        self.assertTrue(mock_exec.called)
        args, kwargs = mock_exec.call_args
        self.assertEqual(args[0], "osascript")
        self.assertEqual(args[1], "-e")
        script = args[2]
        cmd_arg = args[3]
        prompt_arg = args[4]

        # Verify Cocoa script configurations
        self.assertIn("use framework \"AppKit\"", script)
        self.assertIn("textView's setEditable:false", script)
        self.assertIn("textView's setSelectable:true", script)
        self.assertIn("monospacedSystemFontOfSize:12.0", script)
        self.assertIn("theAlert's setAccessoryView:scrollView", script)

        # Verify arguments passed cleanly via argv
        self.assertEqual(cmd_arg, "git push origin main")
        self.assertIn("Working Directory: /Users/example/project", prompt_arg)
        self.assertIn("Trajectory: traj-456", prompt_arg)
        self.assertIn("Request: req-123", prompt_arg)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_reports_both_working_directories(self, mock_exec):
        # The dialog must name both the in-container directory and the effective
        # out-of-container working directory so the user can see the mapping.
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"Approve\n", b"")
        proc_mock.returncode = 0
        mock_exec.return_value = proc_mock

        res = await prompt_user_approval_async(
            "git",
            ["push", "origin", "main"],
            cwd="/Users/example/project",
            container_cwd="/home/developer/project",
        )
        self.assertTrue(res)

        prompt_arg = mock_exec.call_args[0][4]
        self.assertIn("In-Container Directory: /home/developer/project", prompt_arg)
        self.assertIn("Out-of-Container Working Directory: /Users/example/project", prompt_arg)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_includes_note(self, mock_exec):
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"Approve\n", b"")
        proc_mock.returncode = 0
        mock_exec.return_value = proc_mock

        res = await prompt_user_approval_async(
            "git",
            ["push", "origin", "main"],
            note="Matched both auto and approval patterns.",
        )
        self.assertTrue(res)

        prompt_arg = mock_exec.call_args[0][4]
        self.assertIn("Note: Matched both auto and approval patterns.", prompt_arg)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_omits_working_directory_when_unknown(self, mock_exec):
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"Approve\n", b"")
        proc_mock.returncode = 0
        mock_exec.return_value = proc_mock

        res = await prompt_user_approval_async("git", ["fetch"])
        self.assertTrue(res)

        prompt_arg = mock_exec.call_args[0][4]
        self.assertNotIn("Working Directory:", prompt_arg)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_cocoa_denial(self, mock_exec):
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"Deny\n", b"")
        proc_mock.returncode = 0
        mock_exec.return_value = proc_mock

        res = await prompt_user_approval_async("git", ["push", "origin", "main"])
        self.assertFalse(res)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_cocoa_failure_triggers_fallback_approval(self, mock_exec):
        # 1st call (Cocoa script) fails with error
        proc_cocoa = AsyncMock()
        proc_cocoa.communicate.return_value = (b"", b"Cocoa error: framework not available\n")
        proc_cocoa.returncode = 1

        # 2nd call (Fallback display dialog) succeeds
        proc_fallback = AsyncMock()
        proc_fallback.communicate.return_value = (
            b"button returned:Approve, text returned:git push origin main\n",
            b"",
        )
        proc_fallback.returncode = 0

        mock_exec.side_effect = [proc_cocoa, proc_fallback]

        res = await prompt_user_approval_async(
            "git", ["push", "origin", "main"], cwd="/Users/example/project"
        )
        self.assertTrue(res)
        self.assertEqual(mock_exec.call_count, 2)

        fallback_args = mock_exec.call_args_list[1][0]
        fallback_script = fallback_args[2]
        self.assertIn("display dialog", fallback_script)
        self.assertIn('default answer "git push origin main"', fallback_script)
        self.assertIn("Working Directory: /Users/example/project", fallback_script)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_fallback_denial_with_approve_in_command(self, mock_exec):
        proc_cocoa = AsyncMock()
        proc_cocoa.communicate.return_value = (b"", b"error")
        proc_cocoa.returncode = 1

        proc_fallback = AsyncMock()
        proc_fallback.communicate.return_value = (
            b'button returned:Deny, text returned:git commit -m "Approve release"\n',
            b"",
        )
        proc_fallback.returncode = 0

        mock_exec.side_effect = [proc_cocoa, proc_fallback]

        res = await prompt_user_approval_async(
            "git", ["commit", "-m", "Approve release"]
        )
        self.assertFalse(res)

    @patch("asyncio.create_subprocess_exec")
    async def test_prompt_all_fail(self, mock_exec):
        proc_mock = AsyncMock()
        proc_mock.communicate.return_value = (b"", b"canceled\n")
        proc_mock.returncode = 1
        mock_exec.return_value = proc_mock

        res = await prompt_user_approval_async("git", ["fetch"])
        self.assertFalse(res)

    @patch("asyncio.create_subprocess_exec", side_effect=OSError("osascript not found"))
    async def test_prompt_exception_handled(self, mock_exec):
        res = await prompt_user_approval_async("git", ["fetch"])
        self.assertFalse(res)


class TestPidFileAndStartup(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_pid_")
        self.test_pid_path = os.path.join(self.tmpdir, "host-bridge.pid")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cleanup_preserves_other_pid(self):
        with open(self.test_pid_path, "w", encoding="utf-8") as f:
            f.write("99999999\n")
        with patch("host_exec_daemon.PID_FILE_PATH", self.test_pid_path):
            cleanup()
        self.assertTrue(os.path.exists(self.test_pid_path))
        with open(self.test_pid_path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read().strip(), "99999999")

    def test_cleanup_removes_own_pid(self):
        with open(self.test_pid_path, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        with patch("host_exec_daemon.PID_FILE_PATH", self.test_pid_path):
            cleanup()
        self.assertFalse(os.path.exists(self.test_pid_path))

    def test_main_handles_eaddrinuse_cleanly(self):
        import errno
        async def mock_main_async():
            raise OSError(errno.EADDRINUSE, "address already in use")
        with patch("host_exec_daemon.main_async", side_effect=mock_main_async):
            with self.assertRaises(SystemExit) as cm:
                main()
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
