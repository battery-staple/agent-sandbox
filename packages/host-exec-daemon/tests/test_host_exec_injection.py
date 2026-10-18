#!/usr/bin/env python3
"""
Adversarial regression tests for the host-exec whitelist bridge.

These tests pin the *architecture* of host command execution, not just the
individual whitelist regexes. The property under test is:

    A whitelisted command can never cause an attacker-controlled secondary
    command to run, even when the agent controls the command name, the
    argument vector, the working directory and every extra JSON field.

The property holds because of four independent layers, each with its own test
class below:

  1. AUTH   - the request is HMAC-signed over (command, args, cwd) and the
              signature is verified before the command is dispatched or any
              policy is evaluated, including for the ``__list__`` query.
  2. NAMING - the command is an *exact dict-key lookup*, never a regex and
              never a shell word. ``sw_vers && rm -rf /`` is not a key.
  3. ARGV   - arguments are transported as a JSON array and coerced element
              by element to strings; they are never joined into a command
              line for execution. Shell metacharacters (``&&``, ``;``, ``|``,
              backticks, ``$(...)``, newlines) are inert literal argv elements.
  4. EXEC   - the process is spawned with ``asyncio.create_subprocess_exec``
              (an argv vector), never through a shell.

Whenever behaviour is observable, the test runs a *real* subprocess so the
proof is behavioural rather than "the mock was called". A marker file that a
real shell would have created is checked for absence; a Python argv probe
prints the exact argument vector the child actually received.

The gaps this suite once recorded as executable tripwires (argument-boundary
matching, workspace confinement, AppleScript fallback safety and wildcard bind
exposure) are now fixed and are covered by the focused test modules added
alongside those fixes. See ``design/host_exec_injection_test_plan.md``.
"""

import asyncio
import hashlib
import hmac
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import yaml

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

import host_exec_daemon

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DAEMON_SOURCE_PATH = os.path.join(PACKAGE_DIR, "host_exec_daemon.py")
CLIENT_SOURCE_PATH = os.path.join(REPO_DIR, "guest", "bin", "host-exec")

# A binary that exists on both Linux (CI) and macOS.
ECHO_BIN = shutil.which("echo") or "/bin/echo"

# Prints its own argv[1:] as JSON so the test can prove exactly what was passed.
ARGV_PROBE = "import sys, json; sys.stdout.write(json.dumps(sys.argv[1:]))"


def _load_client_module(module_name: str):
    """Import the extensionless guest/bin/host-exec client script."""
    loader = importlib.machinery.SourceFileLoader(module_name, CLIENT_SOURCE_PATH)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class _InjectionHarness(unittest.IsolatedAsyncioTestCase):
    """Boots a real TCP host-exec daemon against a throwaway whitelist."""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_host_exec_injection_")
        self.state_dir = os.path.join(self.temp_dir, "state")
        self.ipc_dir = os.path.join(self.state_dir, "ipc")
        os.makedirs(self.ipc_dir, exist_ok=True)

        self.secret = "injection-test-secret-0123456789abcdef"
        secret_path = os.path.join(self.ipc_dir, "auth_secret.key")
        with open(secret_path, "w", encoding="utf-8") as f:
            f.write(self.secret)

        # `(?s)` (DOTALL) is used for the deliberately permissive entries so the
        # argument policy never masks the argv/exec property being tested.
        self.whitelist = {
            "allowed_workspaces": [self.temp_dir],
            "allowed_commands": {
                "echo_any": {
                    "binary_path": ECHO_BIN,
                    "auto_args_regex": "(?s)^.*$",
                    "description": "Real echo, accepts any argument",
                },
                "echo_strict": {
                    "binary_path": ECHO_BIN,
                    "auto_args_regex": "^-n$",
                    "description": "Real echo, single exact flag",
                },
                "echo_default_dot": {
                    "binary_path": ECHO_BIN,
                    "auto_args_regex": "^.*$",
                    "description": "Real echo under the common non-DOTALL policy",
                },
                "join_probe": {
                    "binary_path": sys.executable,
                    "auto_args_regex": "(?s)^.*$",
                    "description": "Python argv probe",
                },
                "gated": {
                    "binary_path": ECHO_BIN,
                    "approval_args_regex": "(?s)^.*$",
                    "description": "Always requires interactive approval",
                },
                "overlap": {
                    "binary_path": ECHO_BIN,
                    "auto_args_regex": "(?s)^.*$",
                    "approval_args_regex": "(?s)^.*$",
                    "description": "Both patterns match: fail-safe approval",
                },
                "empty_only": {
                    "binary_path": ECHO_BIN,
                    "auto_args_regex": "^$",
                    "description": "Only empty argument vector allowed",
                },
            },
        }
        self.whitelist_path = os.path.join(self.state_dir, "whitelist.yaml")
        with open(self.whitelist_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.whitelist, f)

        self._originals = {
            "WHITELIST_PATH": host_exec_daemon.WHITELIST_PATH,
            "IPC_DIR": host_exec_daemon.IPC_DIR,
            "STATE_DIR": host_exec_daemon.STATE_DIR,
            "cached": host_exec_daemon._cached_whitelist,
            "mtime": host_exec_daemon._last_loaded_mtime,
            "path": host_exec_daemon._last_loaded_path,
        }
        host_exec_daemon.WHITELIST_PATH = self.whitelist_path
        host_exec_daemon.IPC_DIR = self.ipc_dir
        host_exec_daemon.STATE_DIR = self.state_dir
        host_exec_daemon._cached_whitelist = None
        host_exec_daemon._last_loaded_mtime = 0.0
        host_exec_daemon._last_loaded_path = None

        self.server_instance = host_exec_daemon.HostExecServer(self.secret)
        self.server = await asyncio.start_server(
            self.server_instance.handle_client, "127.0.0.1", 0
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server_instance.terminate_active_processes()
        self.server.close()
        await self.server.wait_closed()

        host_exec_daemon.WHITELIST_PATH = self._originals["WHITELIST_PATH"]
        host_exec_daemon.IPC_DIR = self._originals["IPC_DIR"]
        host_exec_daemon.STATE_DIR = self._originals["STATE_DIR"]
        host_exec_daemon._cached_whitelist = self._originals["cached"]
        host_exec_daemon._last_loaded_mtime = self._originals["mtime"]
        host_exec_daemon._last_loaded_path = self._originals["path"]

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -- helpers ---------------------------------------------------------

    def _token(self, command, args, cwd) -> str:
        """Recompute the HMAC exactly like the guest client does."""
        payload = {"command": command, "args": args, "cwd": cwd}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()

    async def _send_raw(self, request: dict) -> dict:
        """Send an arbitrary JSON object and return the daemon's response."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write((json.dumps(request) + "\n").encode("utf-8"))
        await writer.drain()
        raw_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        if not raw_line:
            return {}
        return json.loads(raw_line.decode("utf-8"))

    async def _send_raw_bytes(self, data: bytes) -> dict:
        """Send raw bytes (for duplicate-key / encoding / oversize probes)."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(data)
        await writer.drain()
        raw_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        if not raw_line:
            return {}
        return json.loads(raw_line.decode("utf-8", errors="replace"))

    async def _send_request(self, command, args, cwd=None, extra=None) -> dict:
        cwd = cwd or self.temp_dir
        request = {
            "command": command,
            "args": args,
            "cwd": cwd,
            "token": self._token(command, args, cwd),
            "request_id": f"req-{os.urandom(4).hex()}",
            "trajectory_id": "injection-traj",
        }
        if extra:
            request.update(extra)
        return await self._send_raw(request)

    def _marker(self, name: str) -> str:
        """A path a shell-injection would create; it must never exist."""
        return os.path.join(self.temp_dir, f"PWNED_{name}")

    def _probe_args(self, *payload) -> list:
        return ["-c", ARGV_PROBE, *payload]


class TestArgumentVectorIsInert(_InjectionHarness):
    """Layer 3/4: shell metacharacters stay literal argv elements."""

    async def test_ampersand_is_a_literal_argument(self):
        marker = self._marker("amp")
        payload = ["safe", "&&", "touch", marker]

        res = await self._send_request("echo_any", payload)

        self.assertEqual(res.get("status"), "success")
        # The real child received `&&` as data, echoed it back verbatim...
        self.assertIn("safe && touch " + marker, res.get("stdout", ""))
        # ...and the secondary command never ran.
        self.assertFalse(os.path.exists(marker), "&& was interpreted by a shell!")

    async def test_ampersand_survives_in_exact_argv(self):
        marker = self._marker("amp_argv")
        payload = ["safe", "&&", "touch", marker]

        res = await self._send_request("join_probe", self._probe_args(*payload))

        self.assertEqual(res.get("status"), "success")
        # The Python probe reports the precise vector it was exec'd with.
        self.assertEqual(json.loads(res.get("stdout", "[]")), payload)
        self.assertFalse(os.path.exists(marker))

    async def test_all_shell_metacharacter_vectors_are_inert(self):
        """Every classic injection vector must arrive as one literal argument."""
        vectors = [
            [";", "touch"],
            ["|", "touch"],
            ["&&", "touch"],
            ["`touch"],
            ["$(touch"],
            ["&", "touch"],
            [">"],
            ["<"],
            ["$IFS"],
            ["${IFS}"],
        ]
        for index, prefix in enumerate(vectors):
            marker = self._marker(f"vec{index}")
            payload = [*prefix, marker]
            with self.subTest(vector=prefix):
                res = await self._send_request("join_probe", self._probe_args(*payload))
                self.assertEqual(res.get("status"), "success")
                self.assertEqual(json.loads(res.get("stdout", "[]")), payload)
                self.assertFalse(os.path.exists(marker), f"vector {prefix} escaped to a shell")

    async def test_newlines_in_arguments_are_literal(self):
        """A newline must not start a second command."""
        marker = self._marker("newline")
        payload = [f"safe\ntouch {marker}", f"line1\n&& touch {marker}"]

        res = await self._send_request("join_probe", self._probe_args(*payload))

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(json.loads(res.get("stdout", "[]")), payload)
        self.assertFalse(os.path.exists(marker))

    async def test_single_metachar_argument_without_command_word(self):
        marker = self._marker("solo")
        # A lone `$(touch path)` is a single argv element, not a substitution.
        res = await self._send_request("join_probe", self._probe_args(f"$(touch {marker})"))
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(json.loads(res.get("stdout", "[]")), [f"$(touch {marker})"])
        self.assertFalse(os.path.exists(marker))


class TestExecutionUsesArgvNotShell(_InjectionHarness):
    """Layer 4: the spawn primitive is exactly `[binary_path] + args`."""

    async def test_spawned_vector_is_exact_and_has_no_shell_flag(self):
        marker = self._marker("mock")
        payload = ["safe", "&&", "touch", marker]

        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b"ok", b"")
            proc.returncode = 0
            mock_exec.return_value = proc

            res = await self._send_request("echo_any", payload)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(mock_exec.call_count, 1)
        called = mock_exec.call_args
        # Exact argv vector: the binary then each original element untouched.
        self.assertEqual(called.args, (ECHO_BIN, "safe", "&&", "touch", marker))
        # No shell is ever requested.
        self.assertNotIn("shell", called.kwargs)
        self.assertIsNot(called.kwargs.get("shell"), True)

    async def test_no_shell_spawn_primitive_exists_in_source(self):
        with open(DAEMON_SOURCE_PATH, "r", encoding="utf-8") as f:
            source = f.read()
        forbidden = [
            "create_subprocess_shell",
            "shell=True",
            "shell = True",
            "os.system(",
            "os.popen(",
            "subprocess.Popen",
            "subprocess.run(",
            "subprocess.call(",
            "subprocess.check_output(",
            "pty.spawn",
            "os.spawn",
            "do shell script",
        ]
        for token in forbidden:
            with self.subTest(token=token):
                self.assertNotIn(
                    token,
                    source,
                    f"shell-capable primitive {token!r} appeared in the daemon",
                )
        # Positive control: the safe primitive is what is actually used.
        self.assertIn("create_subprocess_exec", source)


class TestCommandNameIsExactKeyLookup(_InjectionHarness):
    """Layer 2: the command is a dict key, never a shell word or regex."""

    async def test_command_name_cannot_smuggle_a_second_command(self):
        marker = self._marker("name")
        attempts = [
            f"echo_any && touch {marker}",
            f"echo_any; touch {marker}",
            f"echo_any|touch {marker}",
            f"echo_any\ntouch {marker}",
            f"echo_any `touch {marker}`",
            f"echo_any $(touch {marker})",
            "echo_any ",  # trailing whitespace is not trimmed
            " echo_any",  # leading whitespace
            "echo_any/../echo_any",
            "/bin/sh",
            "sh",
            "../../bin/sh",
        ]
        for attempt in attempts:
            with self.subTest(command=attempt):
                res = await self._send_request(attempt, [])
                self.assertEqual(res.get("status"), "error")
                self.assertEqual(res.get("error_type"), "not_whitelisted")
                self.assertFalse(os.path.exists(marker))

    async def test_command_name_is_case_sensitive(self):
        res = await self._send_request("ECHO_ANY", [])
        self.assertEqual(res.get("error_type"), "not_whitelisted")

    async def test_command_name_type_confusion_is_rejected(self):
        # A JSON list is stringified by the daemon and is therefore not a key.
        res = await self._send_raw(
            {
                "command": ["echo_any"],
                "args": [],
                "cwd": self.temp_dir,
                "token": self._token(["echo_any"], [], self.temp_dir),
            }
        )
        self.assertEqual(res.get("error_type"), "not_whitelisted")

    async def test_alias_resolves_to_configured_binary_not_argv0(self):
        # `join_probe` is a name, not a binary; the daemon must run sys.executable.
        marker = self._marker("alias")
        res = await self._send_request("join_probe", self._probe_args("alias", "&&", marker))
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(json.loads(res.get("stdout", "[]")), ["alias", "&&", marker])
        self.assertFalse(os.path.exists(marker))


class TestHMACAuthentication(_InjectionHarness):
    """Layer 1: nothing happens before the signature is verified."""

    async def test_missing_and_empty_token_rejected(self):
        marker = self._marker("notoken")
        for token in (None, ""):
            with self.subTest(token=token):
                request = {
                    "command": "echo_any",
                    "args": ["safe"],
                    "cwd": self.temp_dir,
                    "token": token,
                }
                res = await self._send_raw(request)
                self.assertEqual(res.get("status"), "error")
                self.assertIn("Authentication failed", res.get("message", ""))
                self.assertFalse(os.path.exists(marker))

    async def test_tampered_args_after_signing_rejected(self):
        marker = self._marker("tampered")
        signed_args = ["safe"]
        request = {
            "command": "echo_any",
            "args": ["safe", "&&", "touch", marker],
            "cwd": self.temp_dir,
            "token": self._token("echo_any", signed_args, self.temp_dir),
        }
        res = await self._send_raw(request)
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Authentication failed", res.get("message", ""))
        self.assertFalse(os.path.exists(marker))

    async def test_token_not_transferable_across_commands(self):
        token = self._token("echo_any", ["safe"], self.temp_dir)
        res = await self._send_raw(
            {"command": "join_probe", "args": ["safe"], "cwd": self.temp_dir, "token": token}
        )
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Authentication failed", res.get("message", ""))

    async def test_token_not_transferable_across_cwd(self):
        token = self._token("echo_any", [], self.temp_dir)
        res = await self._send_raw(
            {"command": "echo_any", "args": [], "cwd": "/", "token": token}
        )
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Authentication failed", res.get("message", ""))

    async def test_introspection_also_requires_authentication(self):
        res = await self._send_raw(
            {"command": "__list__", "args": [], "cwd": self.temp_dir, "token": "deadbeef"}
        )
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Authentication failed", res.get("message", ""))

    async def test_extra_json_fields_cannot_redirect_the_spawn(self):
        # An attacker-controlled extra field must not be able to redirect the
        # command: the spawn vector is asserted, not merely the exit status.
        marker = self._marker("extra")
        extra = {
            "shell": True,
            "command_line": f"echo_any && touch {marker}",
            "argv": ["sh", "-c", f"touch {marker}"],
            "binary_path": "/bin/sh",
            "env": {"EVIL": "1"},
            "executable": "/bin/sh",
        }
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            proc = AsyncMock()
            proc.communicate.return_value = (b"safe\n", b"")
            proc.returncode = 0
            mock_exec.return_value = proc
            res = await self._send_request("echo_any", ["safe"], extra=extra)

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(mock_exec.call_args.args, (ECHO_BIN, "safe"))
        kwargs = mock_exec.call_args.kwargs
        for forbidden in ("shell", "env", "executable"):
            self.assertNotIn(forbidden, kwargs)
        self.assertFalse(os.path.exists(marker))


class TestArgumentPolicyFullMatch(_InjectionHarness):
    """Layer 3: the policy regex is anchored with re.fullmatch on argv only."""

    async def test_strict_regex_rejects_prefix_suffix_and_extra_args(self):
        # `echo_strict` allows exactly `-n`.
        accepted = await self._send_request("echo_strict", ["-n"])
        self.assertEqual(accepted.get("status"), "success")

        marker = self._marker("strict")
        rejected = [
            ["-n", "x"],
            ["-n", "&&", "touch", marker],
            ["x", "-n"],
            ["-n "],
            ["-n\n"],
            ["-N"],
        ]
        for args in rejected:
            with self.subTest(args=args):
                res = await self._send_request("echo_strict", args)
                self.assertEqual(res.get("status"), "error")
                self.assertEqual(res.get("error_type"), "args_violation")
                self.assertFalse(os.path.exists(marker))

    async def test_args_that_match_nothing_are_never_executed(self):
        marker = self._marker("nomatch")
        res = await self._send_request("empty_only", ["touch", marker])
        self.assertEqual(res.get("error_type"), "args_violation")
        self.assertFalse(os.path.exists(marker))

    async def test_empty_vector_matches_empty_only_policy(self):
        res = await self._send_request("empty_only", [])
        self.assertEqual(res.get("status"), "success")

    async def test_standard_dot_star_policy_does_not_match_newlines(self):
        # `^.*$` without DOTALL cannot match a newline, so multiline payloads
        # are rejected outright under the common policy shape.
        marker = self._marker("dotstar")
        res = await self._send_request("echo_default_dot", [f"line1\ntouch {marker}"])
        self.assertEqual(res.get("error_type"), "args_violation")
        self.assertFalse(os.path.exists(marker))


class TestApprovalGate(_InjectionHarness):
    """Layer 3/4: approval shows and executes the original argv, never a string."""

    async def test_approval_receives_original_argv_and_executes_it_literally(self):
        marker = self._marker("approved")
        payload = ["safe", "&&", "touch", marker]
        captured = {}

        async def fake_prompt(command_name, args, req_id="", traj_id="", cwd="", container_cwd="", note=""):
            captured["command_name"] = command_name
            captured["args"] = list(args)
            captured["cmd_display_is_argv"] = args == payload
            return True

        original = host_exec_daemon.prompt_user_approval_async
        host_exec_daemon.prompt_user_approval_async = fake_prompt
        try:
            res = await self._send_request("gated", payload)
        finally:
            host_exec_daemon.prompt_user_approval_async = original

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(captured["args"], payload)
        self.assertTrue(captured["cmd_display_is_argv"])
        self.assertIn("safe && touch " + marker, res.get("stdout", ""))
        self.assertFalse(os.path.exists(marker))

    async def test_denied_approval_executes_nothing(self):
        marker = self._marker("denied")
        payload = ["safe", "&&", "touch", marker]

        async def deny(*args, **kwargs):
            return False

        original = host_exec_daemon.prompt_user_approval_async
        host_exec_daemon.prompt_user_approval_async = deny
        try:
            res = await self._send_request("gated", payload)
        finally:
            host_exec_daemon.prompt_user_approval_async = original

        self.assertEqual(res.get("error_type"), "denied_by_user")
        self.assertFalse(os.path.exists(marker))

    async def test_overlapping_patterns_require_approval(self):
        called = {"n": 0}

        async def approve(*args, **kwargs):
            called["n"] += 1
            return True

        original = host_exec_daemon.prompt_user_approval_async
        host_exec_daemon.prompt_user_approval_async = approve
        try:
            res = await self._send_request("overlap", ["-n", "safe"])
        finally:
            host_exec_daemon.prompt_user_approval_async = original

        self.assertEqual(res.get("status"), "success")
        self.assertEqual(called["n"], 1)


class TestInputCoercion(_InjectionHarness):
    """Layer 1/3: type confusion in the JSON payload cannot inject arguments."""

    async def test_args_as_string_is_coerced_to_empty_vector(self):
        marker = self._marker("stringargs")
        request = {
            "command": "echo_any",
            "args": f"safe && touch {marker}",
            "cwd": self.temp_dir,
            "token": self._token("echo_any", f"safe && touch {marker}", self.temp_dir),
        }
        res = await self._send_raw(request)
        self.assertEqual(res.get("status"), "success")
        # A non-list `args` yields an empty argv, not a parsed command line.
        self.assertEqual(res.get("stdout", "").strip(), "")
        self.assertFalse(os.path.exists(marker))

    async def test_args_as_dict_is_coerced_to_empty_vector(self):
        res = await self._send_raw(
            {
                "command": "echo_any",
                "args": {"exec": "touch /tmp/nope"},
                "cwd": self.temp_dir,
                "token": self._token("echo_any", {"exec": "touch /tmp/nope"}, self.temp_dir),
            }
        )
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("stdout", "").strip(), "")

    async def test_nested_args_are_stringified_not_flattened(self):
        marker = self._marker("nested")
        payload = [["&&", f"touch {marker}"]]
        res = await self._send_request("join_probe", self._probe_args(*payload))
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(json.loads(res.get("stdout", "[]")), [str(payload[0])])
        self.assertFalse(os.path.exists(marker))

    async def test_cwd_cannot_inject_commands(self):
        # cwd is a distinct field, never concatenated into argv. A directory
        # whose *name* contains shell metacharacters is still just a path.
        marker = self._marker("cwd")
        weird_dir = os.path.join(self.temp_dir, f"weird && touch {marker}")
        os.makedirs(weird_dir, exist_ok=True)
        res = await self._send_request(
            "join_probe", self._probe_args("cwdtest"), cwd=weird_dir
        )
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(json.loads(res.get("stdout", "[]")), ["cwdtest"])
        self.assertFalse(os.path.exists(marker))

    async def test_unresolvable_cwd_is_rejected_by_workspace_confinement(self):
        # A bogus cwd falls back to the host home, which is outside the
        # configured workspace, so the request is rejected rather than run.
        marker = self._marker("cwd_fallback")
        bogus_cwd = f"/nonexistent-{os.urandom(4).hex()} && touch {marker}"
        res = await self._send_request("join_probe", self._probe_args("x"), cwd=bogus_cwd)
        self.assertEqual(res.get("error_type"), "cwd_not_allowed")
        self.assertFalse(os.path.exists(marker))

    async def test_malformed_json_is_rejected(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(b"{not json\n")
        await writer.drain()
        raw_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        res = json.loads(raw_line.decode("utf-8"))
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Malformed JSON", res.get("message", ""))


class TestClientNeverBuildsAShellString(unittest.TestCase):
    """The guest client sends structured fields; it must never shell-join them."""

    @classmethod
    def setUpClass(cls):
        cls.client = _load_client_module("host_exec_client_injection")

    def _run_client(self, argv):
        captured = {}

        class FakeSock:
            def sendall(self, data):
                captured["request"] = json.loads(data.decode("utf-8").strip())

            def recv(self, _n):
                return b'{"status":"success","stdout":"","stderr":"","returncode":0}\n'

            def close(self):
                pass

        originals = {
            "connect": self.client.connect_to_daemon,
            "secret": self.client.get_auth_secret,
            "validate": self.client.validate_runtime_config,
        }
        self.client.connect_to_daemon = lambda: FakeSock()
        self.client.get_auth_secret = lambda: "client-secret"
        self.client.validate_runtime_config = lambda: None
        try:
            with patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit) as cm:
                    self.client.main()
            self.assertEqual(cm.exception.code, 0)
        finally:
            self.client.connect_to_daemon = originals["connect"]
            self.client.get_auth_secret = originals["secret"]
            self.client.validate_runtime_config = originals["validate"]
        return captured["request"]

    def test_arguments_are_transmitted_as_separate_elements(self):
        request = self._run_client(
            ["host-exec", "echo_any", "safe", "&&", "touch", "/tmp/x"]
        )
        self.assertEqual(request["command"], "echo_any")
        self.assertEqual(request["args"], ["safe", "&&", "touch", "/tmp/x"])
        self.assertIsInstance(request["args"], list)

    def test_list_query_is_structured_too(self):
        request = self._run_client(["host-exec", "--list"])
        self.assertEqual(request["command"], "__list__")
        self.assertEqual(request["args"], [])

    def test_client_source_has_no_subprocess_primitives(self):
        with open(CLIENT_SOURCE_PATH, "r", encoding="utf-8") as f:
            source = f.read()
        for token in ["subprocess", "os.system(", "os.popen(", "create_subprocess_shell"]:
            with self.subTest(token=token):
                self.assertNotIn(token, source)


class TestShippedWhitelistRegexesAreLinear(unittest.TestCase):
    """Guard the default policy against catastrophic backtracking (ReDoS)."""

    def setUp(self):
        self.default_path = os.path.join(REPO_DIR, "config", "whitelist.default.yaml")
        with open(os.path.abspath(self.default_path), "r", encoding="utf-8") as f:
            self.config = yaml.safe_load(f)

    def test_default_regexes_match_adversarial_input_quickly(self):
        adversarial_inputs = [
            "a" * 100000,
            "-" + "a" * 100000,
            "http://" + "a" * 100000 + " " * 1000,
            "--remote-debugging-port=" + "9" * 100000,
            "(" * 50000,
        ]
        for name, policy in self.config.get("allowed_commands", {}).items():
            for key in ("auto_args_regex", "approval_args_regex"):
                regex = policy.get(key)
                if not regex:
                    continue
                compiled = host_exec_daemon.re.compile(regex)
                for sample in adversarial_inputs:
                    with self.subTest(command=name, regex=key, sample_len=len(sample)):
                        start = time.monotonic()
                        compiled.fullmatch(sample)
                        elapsed = time.monotonic() - start
                        self.assertLess(
                            elapsed,
                            0.5,
                            f"regex {regex!r} for {name!r} took {elapsed:.2f}s (possible ReDoS)",
                        )


class TestServerResilience(_InjectionHarness):
    """Malformed input must fail cleanly without executing or crashing."""

    async def test_nul_byte_in_args_returns_clean_error(self):
        marker = self._marker("nul")
        # A shell would treat the NUL-laden string as one word; here the OS
        # rejects it and the daemon must surface a clean error, not a crash.
        res = await self._send_request("echo_any", ["safe\x00&& touch " + marker])
        self.assertEqual(res.get("status"), "error")
        self.assertFalse(os.path.exists(marker))

        # The server must still be healthy for the next client.
        follow_up = await self._send_request("echo_any", ["still-alive"])
        self.assertEqual(follow_up.get("status"), "success")

    async def test_oversize_request_line_is_rejected_cleanly(self):
        marker = self._marker("oversize")
        huge = "A" * 200_000 + " && touch " + marker
        res = await self._send_request("echo_any", [huge])
        self.assertEqual(res.get("status"), "error")
        self.assertFalse(os.path.exists(marker))

        follow_up = await self._send_request("echo_any", ["after-oversize"])
        self.assertEqual(follow_up.get("status"), "success")

    async def test_duplicate_json_keys_use_the_same_parse_for_sign_and_exec(self):
        # The last duplicate wins in json.loads. Because the *same* parsed
        # value is signed and dispatched, a duplicate cannot create a
        # signature/execution differential. A token for the winning command
        # still fails the whitelist lookup.
        marker = self._marker("dup")
        cwd = self.temp_dir
        token = self._token("sh", ["-c", f"touch {marker}"], cwd)
        raw = (
            '{"command": "echo_any", '
            '"args": ["-c", "touch %s"], '
            '"cwd": "%s", '
            '"token": "%s", '
            '"command": "sh"}'
        ) % (marker, cwd, token)
        res = await self._send_raw_bytes((raw + "\n").encode("utf-8"))
        self.assertEqual(res.get("error_type"), "not_whitelisted")
        self.assertFalse(os.path.exists(marker))

    async def test_non_utf8_request_bytes_do_not_crash_the_server(self):
        res = await self._send_raw_bytes(b'{"command": "echo_any", "args": ["\xff\xfe"]}\n')
        self.assertEqual(res.get("status"), "error")
        follow_up = await self._send_request("echo_any", ["ok"])
        self.assertEqual(follow_up.get("status"), "success")


class TestHMACCanonicalization(_InjectionHarness):
    """The signature must be computed over the *same* value that is executed."""

    async def test_token_over_list_rejected_when_wire_args_is_string(self):
        # Sign-what-you-execute: a token that does not cover the literal wire
        # value must not authenticate, even if a coerced form would have run.
        cwd = self.temp_dir
        request = {
            "command": "echo_any",
            "args": "safe",
            "cwd": cwd,
            "token": self._token("echo_any", ["safe"], cwd),
        }
        res = await self._send_raw(request)
        self.assertEqual(res.get("status"), "error")
        self.assertIn("Authentication failed", res.get("message", ""))

    async def test_null_args_are_signed_and_coerced_consistently(self):
        cwd = self.temp_dir
        request = {
            "command": "echo_any",
            "args": None,
            "cwd": cwd,
            "token": self._token("echo_any", None, cwd),
        }
        res = await self._send_raw(request)
        # null is a validly-signed value; it coerces to an empty argv.
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(res.get("stdout", "").strip(), "")

    async def test_non_hex_and_wrong_length_tokens_rejected(self):
        for token in ["zzzz", "deadbeef", "A" * 64, "0" * 63]:
            with self.subTest(token=token):
                res = await self._send_raw(
                    {"command": "echo_any", "args": [], "cwd": self.temp_dir, "token": token}
                )
                self.assertEqual(res.get("status"), "error")
                self.assertIn("Authentication failed", res.get("message", ""))

    async def test_unicode_and_bidi_arguments_are_literal(self):
        payload = ["safe\u202eevil", "café", "日本語", "emoji-😀"]
        res = await self._send_request("join_probe", self._probe_args(*payload))
        self.assertEqual(res.get("status"), "success")
        self.assertEqual(json.loads(res.get("stdout", "[]")), payload)


class TestApprovalPrimaryPathArgvSafety(_InjectionHarness):
    """The Cocoa approval dialog must pass user text as argv, never as script."""

    async def test_primary_dialog_keeps_user_text_out_of_the_script(self):
        marker = self._marker("apple")
        payload = [
            'safe" & (do shell script "touch ' + marker + '") & "',
            "multi\nline",
        ]

        approve = AsyncMock()
        approve.communicate.return_value = (b"Approve\n", b"")
        approve.returncode = 0
        run = AsyncMock()
        run.communicate.return_value = (b"safe\n", b"")
        run.returncode = 0

        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            mock_exec.side_effect = [approve, run]
            res = await self._send_request("gated", payload)

        self.assertEqual(res.get("status"), "success")
        self.assertFalse(os.path.exists(marker))

        first = mock_exec.call_args_list[0]
        argv = first.args
        self.assertEqual(len(argv), 5)
        self.assertEqual(argv[0], "osascript")
        self.assertEqual(argv[1], "-e")
        # The AppleScript program is a constant: no user data is interpolated
        # into it, so no argument can alter the script.
        self.assertEqual(argv[2], host_exec_daemon.COCOA_DIALOG_SCRIPT)
        self.assertNotIn(marker, argv[2])
        # The payload is present only as inert argv data.
        self.assertIn("do shell script", argv[3])
        self.assertNotIn("shell", first.kwargs)
        self.assertNotIn("shell", mock_exec.call_args_list[1].kwargs)


if __name__ == "__main__":
    unittest.main()
