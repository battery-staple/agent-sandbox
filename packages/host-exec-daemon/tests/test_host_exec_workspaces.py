#!/usr/bin/env python3
"""
Regression tests for workspace confinement.

``allowed_workspaces`` is advertised by ``host-exec --list`` and must actually
constrain where a whitelisted command may run. Containment is evaluated on real
paths so a symlink inside a workspace cannot escape it.
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
from host_exec_daemon import path_within_workspaces


class TestPathWithinWorkspaces(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="test_ws_unit_")
        self.workspace = os.path.join(self.tmp, "ws")
        self.inside = os.path.join(self.workspace, "sub", "dir")
        self.outside = os.path.join(self.tmp, "outside")
        os.makedirs(self.inside)
        os.makedirs(self.outside)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_empty_list_is_unrestricted(self):
        self.assertTrue(path_within_workspaces(self.outside, []))
        self.assertTrue(path_within_workspaces(self.outside, None))

    def test_workspace_root_and_descendants_allowed(self):
        self.assertTrue(path_within_workspaces(self.workspace, [self.workspace]))
        self.assertTrue(path_within_workspaces(self.inside, [self.workspace]))

    def test_outside_rejected(self):
        self.assertFalse(path_within_workspaces(self.outside, [self.workspace]))
        self.assertFalse(path_within_workspaces("/", [self.workspace]))

    def test_sibling_prefix_is_not_contained(self):
        sibling = self.workspace + "-evil"
        os.makedirs(sibling)
        self.assertFalse(path_within_workspaces(sibling, [self.workspace]))

    def test_symlink_escape_is_rejected(self):
        link = os.path.join(self.workspace, "escape")
        os.symlink(self.outside, link)
        self.assertFalse(path_within_workspaces(link, [self.workspace]))

    def test_non_string_entries_are_ignored(self):
        self.assertFalse(path_within_workspaces(self.outside, [None, "", 123, self.workspace]))


class TestWorkspaceEnforcement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_ws_int_")
        self.workspace = os.path.join(self.temp_dir, "workspace")
        self.inside = os.path.join(self.workspace, "project")
        self.outside = os.path.join(self.temp_dir, "outside")
        os.makedirs(self.inside)
        os.makedirs(self.outside)

        self.state_dir = os.path.join(self.temp_dir, "state")
        self.ipc_dir = os.path.join(self.state_dir, "ipc")
        os.makedirs(self.ipc_dir, exist_ok=True)

        self.secret = "workspace-secret-0123456789abcdef"
        with open(os.path.join(self.ipc_dir, "auth_secret.key"), "w", encoding="utf-8") as f:
            f.write(self.secret)

        self.whitelist = {
            "allowed_workspaces": [self.workspace],
            "allowed_commands": {
                "echo": {
                    "binary_path": "/bin/echo",
                    "auto_args_regex": "(?s)^.*$",
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

    async def _send(self, cwd, args=None, command="echo"):
        args = args or []
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

    async def test_cwd_inside_workspace_is_allowed(self):
        res = await self._send(self.inside, ["ok"])
        self.assertEqual(res.get("status"), "success")
        self.assertIn("ok", res.get("stdout", ""))

    async def test_workspace_root_is_allowed(self):
        res = await self._send(self.workspace)
        self.assertEqual(res.get("status"), "success")

    async def test_cwd_outside_workspace_is_rejected(self):
        res = await self._send(self.outside, ["nope"])
        self.assertEqual(res.get("status"), "error")
        self.assertEqual(res.get("error_type"), "cwd_not_allowed")
        # Nothing ran.
        self.assertNotIn("nope", res.get("stdout", ""))

    async def test_root_cwd_is_rejected(self):
        res = await self._send("/", ["nope"])
        self.assertEqual(res.get("error_type"), "cwd_not_allowed")

    async def test_symlink_escape_is_rejected(self):
        link = os.path.join(self.workspace, "escape")
        os.symlink(self.outside, link)
        res = await self._send(link, ["nope"])
        self.assertEqual(res.get("error_type"), "cwd_not_allowed")

    async def test_empty_workspace_list_is_unrestricted(self):
        self.whitelist["allowed_workspaces"] = []
        with open(self.whitelist_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.whitelist, f)
        # Force hot-reload.
        await asyncio.sleep(0.02)
        res = await self._send(self.outside, ["free"])
        self.assertEqual(res.get("status"), "success")
        self.assertIn("free", res.get("stdout", ""))


if __name__ == "__main__":
    unittest.main()
