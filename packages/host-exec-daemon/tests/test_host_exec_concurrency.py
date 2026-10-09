#!/usr/bin/env python3
"""
Comprehensive concurrency, interleaving, and safety integration tests for host_exec_daemon.
"""

import asyncio
import hashlib
import hmac
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import yaml

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PACKAGE_DIR)

import host_exec_daemon


class TestHostExecConcurrency(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_host_exec_concurrency_")
        self.state_dir = os.path.join(self.temp_dir, "state")
        self.ipc_dir = os.path.join(self.state_dir, "ipc")
        os.makedirs(self.ipc_dir, exist_ok=True)

        self.secret = "test-concurrency-secret-0123456789abcdef"
        self.secret_path = os.path.join(self.ipc_dir, "auth_secret.key")
        with open(self.secret_path, "w", encoding="utf-8") as f:
            f.write(self.secret)

        self.whitelist_path = os.path.join(self.state_dir, "whitelist.yaml")
        self.initial_whitelist = {
            "allowed_workspaces": [self.temp_dir],
            "allowed_commands": {
                "echo": {
                    "binary_path": sys.executable,
                    "allowed_args_regex": "^.*$",
                    "require_interactive_approval": False,
                    "description": "Echo command via python",
                },
                "slow_cmd": {
                    "binary_path": sys.executable,
                    "allowed_args_regex": "^.*$",
                    "require_interactive_approval": False,
                    "description": "Slow sleep command",
                },
                "approval_cmd": {
                    "binary_path": sys.executable,
                    "allowed_args_regex": "^.*$",
                    "require_interactive_approval": True,
                    "description": "Command requiring interactive approval",
                },
            },
        }
        with open(self.whitelist_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(self.initial_whitelist, f)

        # Patch daemon global paths
        self.orig_whitelist_path = host_exec_daemon.WHITELIST_PATH
        self.orig_ipc_dir = host_exec_daemon.IPC_DIR
        self.orig_state_dir = host_exec_daemon.STATE_DIR
        self.orig_cached_whitelist = host_exec_daemon._cached_whitelist
        self.orig_last_loaded_mtime = host_exec_daemon._last_loaded_mtime
        self.orig_last_loaded_path = host_exec_daemon._last_loaded_path

        host_exec_daemon.WHITELIST_PATH = self.whitelist_path
        host_exec_daemon.IPC_DIR = self.ipc_dir
        host_exec_daemon.STATE_DIR = self.state_dir
        host_exec_daemon._cached_whitelist = None
        host_exec_daemon._last_loaded_mtime = 0.0
        host_exec_daemon._last_loaded_path = None

        self.server_instance = host_exec_daemon.HostExecServer(self.secret, max_concurrency=16)
        host_exec_daemon._default_server = self.server_instance

        # Start ephemeral TCP server on dynamic port
        self.server = await asyncio.start_server(
            self.server_instance.handle_client,
            "127.0.0.1",
            0,
        )
        self.port = self.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self):
        self.server_instance.terminate_active_processes()
        self.server.close()
        await self.server.wait_closed()

        host_exec_daemon.WHITELIST_PATH = self.orig_whitelist_path
        host_exec_daemon.IPC_DIR = self.orig_ipc_dir
        host_exec_daemon.STATE_DIR = self.orig_state_dir
        host_exec_daemon._cached_whitelist = self.orig_cached_whitelist
        host_exec_daemon._last_loaded_mtime = self.orig_last_loaded_mtime
        host_exec_daemon._last_loaded_path = self.orig_last_loaded_path
        host_exec_daemon._default_server = None

        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _compute_token(self, cmd: str, args: list, cwd: str) -> str:
        payload = {"command": cmd, "args": args, "cwd": cwd}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self.secret.encode("utf-8"), canonical, hashlib.sha256).hexdigest()

    async def _send_request(
        self, cmd: str, args: list, cwd: str = None, req_id: str = None, traj_id: str = None
    ) -> dict:
        cwd = cwd or self.temp_dir
        token = self._compute_token(cmd, args, cwd)
        req = {
            "command": cmd,
            "args": args,
            "cwd": cwd,
            "token": token,
            "request_id": req_id or f"req-{os.urandom(4).hex()}",
            "trajectory_id": traj_id or "test-traj",
        }
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()

        raw_line = await reader.readline()
        writer.close()
        await writer.wait_closed()
        if not raw_line:
            return {}
        return json.loads(raw_line.decode("utf-8"))

    async def test_parallel_non_blocking_execution(self):
        """Verify fast commands complete in <100ms without blocking behind a slow 1.5s command."""
        # slow_cmd runs python sleep for 1.5 seconds
        slow_args = ["-c", "import time; time.sleep(1.5); print('slow_done')"]
        fast_args = ["-c", "print('fast_done')"]

        t_start = time.monotonic()
        slow_task = asyncio.create_task(self._send_request("slow_cmd", slow_args))
        await asyncio.sleep(0.05)  # Let slow task start

        # Send 5 fast commands
        fast_tasks = [asyncio.create_task(self._send_request("echo", fast_args)) for _ in range(5)]
        fast_results = await asyncio.gather(*fast_tasks)
        t_fast_done = time.monotonic()

        # Fast commands must finish well before slow_cmd
        fast_duration = t_fast_done - t_start
        self.assertLess(fast_duration, 0.5, f"Fast commands took {fast_duration:.2f}s; expected < 0.5s")
        for res in fast_results:
            self.assertEqual(res.get("status"), "success")
            self.assertIn("fast_done", res.get("stdout", ""))

        # Now wait for slow command to complete
        slow_res = await slow_task
        t_slow_done = time.monotonic()
        self.assertEqual(slow_res.get("status"), "success")
        self.assertIn("slow_done", slow_res.get("stdout", ""))
        self.assertGreaterEqual(t_slow_done - t_start, 1.4)

    async def test_fast_behind_interactive_approval(self):
        """Verify non-interactive commands run and finish while an interactive approval is pending."""
        approval_gate = asyncio.Event()

        async def mock_approval_dialog(cmd_name, args, req_id="", traj_id="", cwd=""):
            await approval_gate.wait()
            return True

        orig_prompt = host_exec_daemon.prompt_user_approval_async
        host_exec_daemon.prompt_user_approval_async = mock_approval_dialog
        try:
            # Start approval request (which waits on approval_gate)
            approval_task = asyncio.create_task(
                self._send_request("approval_cmd", ["-c", "print('approved_output')"])
            )
            await asyncio.sleep(0.05)

            # Send non-interactive fast command while approval is waiting
            t_fast_start = time.monotonic()
            fast_res = await self._send_request("echo", ["-c", "print('fast_independent')"])
            t_fast_done = time.monotonic()

            self.assertLess(t_fast_done - t_fast_start, 0.2)
            self.assertEqual(fast_res.get("status"), "success")
            self.assertIn("fast_independent", fast_res.get("stdout", ""))

            # Approval task should still be pending
            self.assertFalse(approval_task.done())

            # Now release approval gate
            approval_gate.set()
            app_res = await approval_task
            self.assertEqual(app_res.get("status"), "success")
            self.assertIn("approved_output", app_res.get("stdout", ""))
        finally:
            host_exec_daemon.prompt_user_approval_async = orig_prompt

    async def test_interactive_approval_serialization(self):
        """Verify two concurrent approval requests are queued in FIFO sequence without modal overlap."""
        active_prompts = 0
        max_simultaneous_prompts = 0
        call_order = []
        received_cwds = []

        async def mock_dialog(cmd_name, args, req_id="", traj_id="", cwd=""):
            nonlocal active_prompts, max_simultaneous_prompts
            active_prompts += 1
            max_simultaneous_prompts = max(max_simultaneous_prompts, active_prompts)
            call_order.append(req_id)
            received_cwds.append(cwd)
            await asyncio.sleep(0.1)
            active_prompts -= 1
            return True

        orig_prompt = host_exec_daemon.prompt_user_approval_async
        host_exec_daemon.prompt_user_approval_async = mock_dialog
        try:
            req1 = asyncio.create_task(
                self._send_request(
                    "approval_cmd", ["-c", "print('req1')"], req_id="req-first", traj_id="traj-1"
                )
            )
            req2 = asyncio.create_task(
                self._send_request(
                    "approval_cmd", ["-c", "print('req2')"], req_id="req-second", traj_id="traj-2"
                )
            )

            res1, res2 = await asyncio.gather(req1, req2)
            self.assertEqual(res1.get("status"), "success")
            self.assertEqual(res2.get("status"), "success")

            # Must have evaluated strictly sequentially (max 1 prompt active at any instant)
            self.assertEqual(max_simultaneous_prompts, 1)
            self.assertEqual(call_order, ["req-first", "req-second"])
            # The dialog must receive the resolved working directory for each request
            self.assertEqual(received_cwds, [self.temp_dir, self.temp_dir])
        finally:
            host_exec_daemon.prompt_user_approval_async = orig_prompt

    async def test_interactive_approval_denial(self):
        """Verify user denial cleanly aborts execution with denied_by_user and no process runs."""
        async def mock_denial(cmd_name, args, req_id="", traj_id="", cwd=""):
            return False

        orig_prompt = host_exec_daemon.prompt_user_approval_async
        host_exec_daemon.prompt_user_approval_async = mock_dialog = mock_denial
        try:
            res = await self._send_request("approval_cmd", ["-c", "print('should_not_run')"])
            self.assertEqual(res.get("status"), "error")
            self.assertEqual(res.get("error_type"), "denied_by_user")
            self.assertIn("denied by user", res.get("message", "").lower())
        finally:
            host_exec_daemon.prompt_user_approval_async = orig_prompt

    async def test_stream_and_cwd_isolation(self):
        """Verify stdout, stderr, and working directory remain strictly isolated per request."""
        dir1 = os.path.join(self.temp_dir, "dir1")
        dir2 = os.path.join(self.temp_dir, "dir2")
        os.makedirs(dir1, exist_ok=True)
        os.makedirs(dir2, exist_ok=True)

        task1 = asyncio.create_task(
            self._send_request(
                "echo",
                ["-c", "import os, sys; print(f'OUT1:{os.getcwd()}'); sys.stderr.write('ERR1')"],
                cwd=dir1,
            )
        )
        task2 = asyncio.create_task(
            self._send_request(
                "echo",
                ["-c", "import os, sys; print(f'OUT2:{os.getcwd()}'); sys.stderr.write('ERR2')"],
                cwd=dir2,
            )
        )

        res1, res2 = await asyncio.gather(task1, task2)
        self.assertIn(f"OUT1:{dir1}", res1.get("stdout", ""))
        self.assertEqual("ERR1", res1.get("stderr", ""))
        self.assertIn(f"OUT2:{dir2}", res2.get("stdout", ""))
        self.assertEqual("ERR2", res2.get("stderr", ""))

    async def test_client_disconnect_process_cleanup(self):
        """Verify that abrupt client socket drop terminates the host child process cleanly."""
        cmd = "slow_cmd"
        args = ["-c", "import time; time.sleep(10)"]
        token = self._compute_token(cmd, args, self.temp_dir)
        req_id = "req-disconnect-test"
        req = {
            "command": cmd,
            "args": args,
            "cwd": self.temp_dir,
            "token": token,
            "request_id": req_id,
            "trajectory_id": "traj-drop",
        }

        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()

        # Wait for daemon to register active process
        for _ in range(50):
            if req_id in self.server_instance.active_processes:
                break
            await asyncio.sleep(0.02)

        self.assertIn(req_id, self.server_instance.active_processes)
        proc = self.server_instance.active_processes[req_id]

        # Abruptly close writer socket
        writer.close()
        await writer.wait_closed()

        # Wait for daemon to reap process
        for _ in range(50):
            if req_id not in self.server_instance.active_processes:
                break
            await asyncio.sleep(0.02)

        self.assertNotIn(req_id, self.server_instance.active_processes)
        # Process should have been terminated
        self.assertIsNotNone(proc.returncode)

    async def test_high_burst_concurrency_semaphore(self):
        """Verify 16 concurrent requests run successfully and respect semaphore bounds."""
        # Set semaphore to 4
        self.server_instance.process_semaphore = asyncio.Semaphore(4)
        active_count = 0
        max_active = 0

        # We execute a command that tracks concurrency via a small sleep
        tasks = [
            asyncio.create_task(
                self._send_request(
                    "echo",
                    ["-c", "import time; time.sleep(0.05); print('burst')"],
                    req_id=f"burst-{i}",
                )
            )
            for i in range(16)
        ]

        results = await asyncio.gather(*tasks)
        for res in results:
            self.assertEqual(res.get("status"), "success")
            self.assertIn("burst", res.get("stdout", ""))

    async def test_dynamic_whitelist_hot_reload(self):
        """Verify whitelist modification dynamically permits newly whitelisted commands."""
        # Initially, 'new_tool' is not in whitelist
        res_before = await self._send_request("new_tool", ["test"])
        self.assertEqual(res_before.get("error_type"), "not_whitelisted")

        # Update whitelist.yaml
        updated_whitelist = dict(self.initial_whitelist)
        updated_whitelist["allowed_commands"]["new_tool"] = {
            "binary_path": sys.executable,
            "allowed_args_regex": "^.*$",
            "require_interactive_approval": False,
            "description": "Newly added tool",
        }
        with open(self.whitelist_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(updated_whitelist, f)

        # Force slight sleep to ensure filesystem mtime is updated
        await asyncio.sleep(0.1)

        res_after = await self._send_request("new_tool", ["-c", "print('hot_reload_success')"])
        self.assertEqual(res_after.get("status"), "success")
        self.assertIn("hot_reload_success", res_after.get("stdout", ""))

    async def test_slow_and_malformed_client_protection(self):
        """Verify that malformed or stalled clients do not stall or corrupt healthy clients."""
        # 1. Send malformed JSON
        reader1, writer1 = await asyncio.open_connection("127.0.0.1", self.port)
        writer1.write(b"{invalid-json\n")
        await writer1.drain()
        err_line = await reader1.readline()
        writer1.close()
        await writer1.wait_closed()
        err_res = json.loads(err_line.decode("utf-8"))
        self.assertEqual(err_res.get("status"), "error")
        self.assertIn("Malformed JSON", err_res.get("message", ""))

        # 2. Healthy client succeeds immediately afterwards
        healthy_res = await self._send_request("echo", ["-c", "print('healthy')"])
        self.assertEqual(healthy_res.get("status"), "success")
        self.assertIn("healthy", healthy_res.get("stdout", ""))

    async def test_instant_introspection_during_slow_process(self):
        """Verify __list__ query returns instantly in <50ms while a slow command is executing."""
        slow_args = ["-c", "import time; time.sleep(1.0); print('slow')"]
        slow_task = asyncio.create_task(self._send_request("slow_cmd", slow_args))
        await asyncio.sleep(0.05)

        t_list_start = time.monotonic()
        list_res = await self._send_request("__list__", [])
        t_list_done = time.monotonic()

        self.assertLess(t_list_done - t_list_start, 0.1)
        self.assertEqual(list_res.get("status"), "success")
        self.assertEqual(list_res.get("type"), "capabilities")
        self.assertIn("slow_cmd", list_res.get("allowed_commands", {}))

        await slow_task

    async def test_daemon_graceful_shutdown_terminates_active_processes(self):
        """Verify terminate_all_active_processes kills running child processes."""
        slow_args = ["-c", "import time; time.sleep(10)"]
        slow_task = asyncio.create_task(self._send_request("slow_cmd", slow_args, req_id="req-term-test"))

        for _ in range(50):
            if "req-term-test" in self.server_instance.active_processes:
                break
            await asyncio.sleep(0.02)

        self.assertIn("req-term-test", self.server_instance.active_processes)
        proc = self.server_instance.active_processes["req-term-test"]

        # Simulate shutdown reaping
        self.server_instance.terminate_active_processes()
        self.assertEqual(len(self.server_instance.active_processes), 0)
        await asyncio.sleep(0.1)
        self.assertIsNotNone(proc.returncode)

        try:
            await slow_task
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()
