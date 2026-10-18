#!/usr/bin/env python3
"""
Regression tests for argument-boundary handling in the host-exec whitelist.

A policy regex must authorize exactly the arguments whose boundaries it
consumes. Because the regex is matched against a newline-joined vector with
``re.DOTALL`` disabled, a trailing wildcard can no longer swallow an extra,
attacker-chosen argument (the ``whitelisted && malicious`` class of bug).
"""

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import unittest

import yaml

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

import host_exec_daemon
from host_exec_daemon import build_args_match_string


class TestBuildArgsMatchString(unittest.TestCase):
    def test_joins_with_newlines(self):
        self.assertEqual(build_args_match_string(["-a", "Simulator"]), "-a\nSimulator")
        self.assertEqual(build_args_match_string([]), "")
        self.assertEqual(build_args_match_string(["--flag=1"]), "--flag=1")

    def test_dot_does_not_span_argument_boundaries(self):
        import re

        joined = build_args_match_string(["--remote-debugging-port=9222", "--load-extension=/x"])
        self.assertIsNone(re.fullmatch(r"--remote-debugging-port=\d+.*", joined))
        # A single element still matches the same pattern.
        self.assertIsNotNone(
            re.fullmatch(
                r"--remote-debugging-port=\d+.*",
                build_args_match_string(["--remote-debugging-port=9222"]),
            )
        )


class TestArgBoundaryEnforcement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_arg_boundaries_")
        self.state_dir = os.path.join(self.temp_dir, "state")
        self.ipc_dir = os.path.join(self.state_dir, "ipc")
        os.makedirs(self.ipc_dir, exist_ok=True)

        self.secret = "arg-boundary-secret-0123456789abcdef"
        with open(os.path.join(self.ipc_dir, "auth_secret.key"), "w", encoding="utf-8") as f:
            f.write(self.secret)

        self.whitelist = {
            "allowed_workspaces": [self.temp_dir],
            "allowed_commands": {
                "port_dotstar": {
                    "binary_path": "/bin/echo",
                    "auto_args_regex": r"^--remote-debugging-port=\d+.*$",
                },
                "port_anchored": {
                    "binary_path": "/bin/echo",
                    "auto_args_regex": r"^--remote-debugging-port=\d+$",
                },
                "two_args": {
                    "binary_path": "/bin/echo",
                    "auto_args_regex": r"^-a\s+Simulator$",
                },
                "any_args": {
                    "binary_path": "/bin/echo",
                    "auto_args_regex": r"(?s)^.*$",
                },
                "empty_ok": {
                    "binary_path": "/bin/echo",
                    "auto_args_regex": r"^.*$",
                },
            },
        }
        self.whitelist_path = os.path.join(self.state_dir, "whitelist.yaml")
        with open(self.whitelist_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.whitelist, f)

        self._originals = {
            k: getattr(host_exec_daemon, k)
            for k in ("WHITELIST_PATH", "IPC_DIR", "STATE_DIR", "_cached_whitelist", "_last_loaded_mtime", "_last_loaded_path")
        }
        host_exec_daemon.WHITELIST_PATH = self.whitelist_path
        host_exec_daemon.IPC_DIR = self.ipc_dir
        host_exec_daemon.STATE_DIR = self.state_dir
        host_exec_daemon._cached_whitelist = None
        host_exec_daemon._last_loaded_mtime = 0.0
        host_exec_daemon._last_loaded_path = None

        self.server_instance = host_exec_daemon.HostExecServer(self.secret)
        self.server = await asyncio.start_server(self.server_instance.handle_client, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server_instance.terminate_active_processes()
        self.server.close()
        await self.server.wait_closed()
        for k, v in self._originals.items():
            setattr(host_exec_daemon, k, v)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _token(self, command, args, cwd):
        payload = {"command": command, "args": args, "cwd": cwd}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()

    async def _send(self, command, args, cwd=None):
        cwd = cwd or self.temp_dir
        req = {
            "command": command,
            "args": args,
            "cwd": cwd,
            "token": self._token(command, args, cwd),
            "request_id": f"req-{os.urandom(4).hex()}",
        }
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()
        raw = await reader.readline()
        writer.close()
        await writer.wait_closed()
        return json.loads(raw.decode("utf-8")) if raw else {}

    async def test_trailing_wildcard_does_not_authorize_extra_flag(self):
        # The vuln: a `.*`-suffixed pattern used to accept an extra argument.
        res = await self._send(
            "port_dotstar", ["--remote-debugging-port=9222", "--load-extension=/tmp/evil"]
        )
        self.assertEqual(res.get("error_type"), "args_violation")

    async def test_trailing_wildcard_still_authorizes_the_single_intended_flag(self):
        res = await self._send("port_dotstar", ["--remote-debugging-port=9222"])
        self.assertEqual(res.get("status"), "success")

    async def test_anchored_policy_rejects_extra_flag(self):
        res = await self._send(
            "port_anchored", ["--remote-debugging-port=9222", "--load-extension=/tmp/evil"]
        )
        self.assertEqual(res.get("error_type"), "args_violation")

    async def test_multi_argument_pattern_must_consume_each_boundary(self):
        ok = await self._send("two_args", ["-a", "Simulator"])
        self.assertEqual(ok.get("status"), "success")
        # A third element is not covered by the pattern.
        bad = await self._send("two_args", ["-a", "Simulator", "--extra"])
        self.assertEqual(bad.get("error_type"), "args_violation")

    async def test_explicit_dotall_opt_in_still_allows_any_vector(self):
        res = await self._send("any_args", ["a", "b", "c"])
        self.assertEqual(res.get("status"), "success")

    async def test_empty_vector_matches_dot_star(self):
        res = await self._send("empty_ok", [])
        self.assertEqual(res.get("status"), "success")

    async def test_empty_vector_rejected_by_flag_pattern(self):
        res = await self._send("port_anchored", [])
        self.assertEqual(res.get("error_type"), "args_violation")


if __name__ == "__main__":
    unittest.main()
