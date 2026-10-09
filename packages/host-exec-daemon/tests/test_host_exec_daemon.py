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
    verify_token,
)


class TestResolveCwd(unittest.TestCase):
    def test_resolve_valid_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(resolve_cwd(tmpdir), tmpdir)

    def test_resolve_nonexistent_directory(self):
        nonexistent = "/path/that/does/not/exist/for/sure_12345"
        self.assertEqual(resolve_cwd(nonexistent), os.path.expanduser("~"))

    def test_resolve_none_or_empty(self):
        self.assertEqual(resolve_cwd(None), os.path.expanduser("~"))
        self.assertEqual(resolve_cwd(""), os.path.expanduser("~"))


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
        policy = {"binary_path": "/usr/bin/open", "allowed_args_regex": "^-a Simulator$"}
        desc = get_command_description("simulator", policy)
        self.assertIn("Run '/usr/bin/open' via alias 'simulator'", desc)
        self.assertIn("-a Simulator", desc)


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
