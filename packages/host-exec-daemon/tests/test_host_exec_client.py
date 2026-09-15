#!/usr/bin/env python3
"""
Unit tests for guest/bin/host-exec client
"""

import importlib.machinery
import importlib.util
import os
import sys
import tempfile
import unittest

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
GUEST_BIN_DIR = os.path.join(REPO_DIR, "guest", "bin")

# Import host-exec script (without .py extension) dynamically
loader = importlib.machinery.SourceFileLoader("host_exec_client", os.path.join(GUEST_BIN_DIR, "host-exec"))
spec = importlib.util.spec_from_loader(loader.name, loader)
host_exec = importlib.util.module_from_spec(spec)
loader.exec_module(host_exec)


class TestHostExecClient(unittest.TestCase):
    def test_format_capabilities_empty(self):
        out = host_exec.format_capabilities({})
        self.assertIn("AVAILABLE HOST COMMANDS", out)
        self.assertIn("(No commands whitelisted yet)", out)

    def test_format_capabilities_populated(self):
        commands = {
            "xcodebuild": {
                "binary_path": "/usr/bin/xcodebuild",
                "allowed_args_regex": "^(-version|-list.*)$",
                "require_interactive_approval": False,
                "description": "Apple Xcode build tool",
            },
            "git-credential-osxkeychain": {
                "binary_path": "/usr/bin/git",
                "allowed_args_regex": "^credential-osxkeychain.*$",
                "require_interactive_approval": True,
                "description": "Keychain helper",
            },
        }
        out = host_exec.format_capabilities(commands)
        self.assertIn("• xcodebuild", out)
        self.assertIn("Apple Xcode build tool", out)
        self.assertIn("Auto (No prompt)", out)
        self.assertIn("• git-credential-osxkeychain", out)
        self.assertIn("Interactive Approval Required (macOS Dialog)", out)

    def test_get_auth_secret_from_env(self):
        orig = os.environ.get("HOST_EXEC_SECRET")
        try:
            os.environ["HOST_EXEC_SECRET"] = "env-secret-123456"
            self.assertEqual(host_exec.get_auth_secret(), "env-secret-123456")
        finally:
            if orig is not None:
                os.environ["HOST_EXEC_SECRET"] = orig
            else:
                os.environ.pop("HOST_EXEC_SECRET", None)

    def test_get_auth_secret_from_file(self):
        orig_env = os.environ.pop("HOST_EXEC_SECRET", None)
        with tempfile.TemporaryDirectory() as tmpdir:
            secret_file = os.path.join(tmpdir, "auth_secret.key")
            with open(secret_file, "w") as f:
                f.write("file-secret-abcdef\n")

            orig_path = host_exec.AUTH_SECRET_FILE
            try:
                host_exec.AUTH_SECRET_FILE = secret_file
                self.assertEqual(host_exec.get_auth_secret(), "file-secret-abcdef")
            finally:
                host_exec.AUTH_SECRET_FILE = orig_path
                if orig_env:
                    os.environ["HOST_EXEC_SECRET"] = orig_env

    def test_runtime_config_requires_host_and_port(self):
        orig_host = host_exec.HOST_EXEC_HOST
        orig_port = host_exec.HOST_EXEC_PORT
        try:
            host_exec.HOST_EXEC_HOST = None
            host_exec.HOST_EXEC_PORT = None
            with self.assertRaisesRegex(ValueError, "HOST_EXEC_HOST"):
                host_exec.validate_runtime_config()

            host_exec.HOST_EXEC_HOST = "host.docker.internal"
            with self.assertRaisesRegex(ValueError, "HOST_EXEC_PORT"):
                host_exec.validate_runtime_config()
        finally:
            host_exec.HOST_EXEC_HOST = orig_host
            host_exec.HOST_EXEC_PORT = orig_port

    def test_runtime_config_rejects_invalid_port(self):
        orig_host = host_exec.HOST_EXEC_HOST
        orig_port = host_exec.HOST_EXEC_PORT
        try:
            host_exec.HOST_EXEC_HOST = "host.docker.internal"
            host_exec.HOST_EXEC_PORT = "not-a-port"
            with self.assertRaisesRegex(ValueError, "integer"):
                host_exec.validate_runtime_config()
        finally:
            host_exec.HOST_EXEC_HOST = orig_host
            host_exec.HOST_EXEC_PORT = orig_port

    def test_connect_to_daemon_offline(self):
        orig_host = host_exec.HOST_EXEC_HOST
        orig_port = host_exec.HOST_EXEC_PORT
        try:
            # Point to an unused local port
            host_exec.HOST_EXEC_HOST = "127.0.0.1"
            host_exec.HOST_EXEC_PORT = 59999
            sock = host_exec.connect_to_daemon()
            self.assertIsNone(sock)
        finally:
            host_exec.HOST_EXEC_HOST = orig_host
            host_exec.HOST_EXEC_PORT = orig_port


if __name__ == "__main__":
    unittest.main()
