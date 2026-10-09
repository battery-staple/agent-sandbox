"""Tests for declarative CLI framework and dynamic tab completion."""
import os
import sys
import tempfile
import unittest
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.cli import SandboxCLI, app, main
from agent_sandbox.config.models import EngineManifest
from agent_sandbox.framework import CLIApp, argument, option


class TestFrameworkCore(unittest.TestCase):
    def test_command_dispatch_and_options(self):
        test_app = CLIApp(name="test-cli")
        executed = {}

        @test_app.command("run", help="Run a task")
        @argument("target", help="Target task")
        @option("-f", "--force", is_flag=True, dest="force", value=True, help="Force execution")
        @option("-v", "--verbose", is_flag=True, dest="verbose", value=True, help="Verbose output")
        def run_handler(target: str, force: bool = False, verbose: bool = False) -> int:
            executed["target"] = target
            executed["force"] = force
            executed["verbose"] = verbose
            return 42

        code = test_app.dispatch(None, ["run", "my-task", "--force"])
        self.assertEqual(code, 42)
        self.assertEqual(executed, {"target": "my-task", "force": True, "verbose": False})

    def test_group_dispatch_and_subcommands(self):
        test_app = CLIApp(name="test-cli")
        results = []

        grp = test_app.group("item", aliases=["it"], default_command="list")

        @grp.command("add")
        @argument("name")
        def item_add(name: str) -> int:
            results.append(("add", name))
            return 0

        @grp.command("list", aliases=["ls"])
        def item_list() -> int:
            results.append(("list",))
            return 0

        # Subcommand dispatch
        self.assertEqual(test_app.dispatch(None, ["item", "add", "widget"]), 0)
        self.assertEqual(results[-1], ("add", "widget"))

        # Alias dispatch
        self.assertEqual(test_app.dispatch(None, ["it", "ls"]), 0)
        self.assertEqual(results[-1], ("list",))

        # Default subcommand dispatch
        self.assertEqual(test_app.dispatch(None, ["item"]), 0)
        self.assertEqual(results[-1], ("list",))

    def test_help_formatting(self):
        test_app = CLIApp(name="test-cli")

        @test_app.command("hello", help="Say hello to someone")
        @argument("name", help="Name to greet")
        def cmd_hello(name: str):
            pass

        help_text = test_app.format_help()
        self.assertIn("test-cli <command> [options]", help_text)
        self.assertIn("hello <name>", help_text)
        self.assertIn("Say hello to someone", help_text)

    def test_completion_top_level_zsh_and_bash(self):
        test_app = CLIApp(name="test-cli")

        @test_app.command("start", aliases=["up"], help="Start engine")
        def cmd_start():
            pass

        grp = test_app.group("workspace", aliases=["ws"], help="Manage workspaces")

        @grp.command("list", help="List workspaces")
        def ws_list():
            pass

        # Zsh completion format: name:description
        zsh_candidates = test_app.complete(None, "zsh", 2, ["test-cli", ""])
        self.assertIn("start:Start engine", zsh_candidates)
        self.assertIn("up:Start engine (alias)", zsh_candidates)
        self.assertIn("workspace:Manage workspaces", zsh_candidates)
        self.assertIn("ws:Manage workspaces (alias)", zsh_candidates)

        # Bash completion format: name
        bash_candidates = test_app.complete(None, "bash", 1, ["test-cli", ""])
        self.assertIn("start", bash_candidates)
        self.assertIn("up", bash_candidates)
        self.assertIn("workspace", bash_candidates)
        self.assertIn("ws", bash_candidates)

    def test_completion_dynamic_resolver(self):
        test_app = CLIApp(name="test-cli")

        def mock_resolver(ctx):
            return ["alpha", "beta", "gamma"]

        @test_app.command("select", help="Select an item")
        @argument("choice", help="Choice", complete=mock_resolver)
        @option("--all", is_flag=True, help="Select all")
        def cmd_select(choice: str, all: bool = False):
            pass

        # Completing positional argument
        candidates = test_app.complete(None, "zsh", 3, ["test-cli", "select", ""])
        self.assertIn("alpha", candidates)
        self.assertIn("beta", candidates)
        self.assertIn("gamma", candidates)

        # Filtering with prefix
        filtered = test_app.complete(None, "zsh", 3, ["test-cli", "select", "be"])
        self.assertEqual(filtered, ["beta"])

        # Completing option flag
        flags = test_app.complete(None, "zsh", 3, ["test-cli", "select", "-"])
        self.assertIn("--all:Select all", flags)

    def test_completion_special_directories_token(self):
        test_app = CLIApp(name="test-cli")

        @test_app.command("mkdir")
        @argument("path", complete="directories")
        def cmd_mkdir(path: str):
            pass

        res = test_app.complete(None, "zsh", 3, ["test-cli", "mkdir", ""])
        self.assertEqual(res, ["__DIRS__"])


class TestSandboxCLICompletions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_framework_cli_")
        self.cli = SandboxCLI(repo_root=self.tmpdir, sandbox_dir=os.path.join(self.tmpdir, "sandbox"))
        self.cli.registry.register(EngineManifest(name="opencode", port=4096, web_url="http://127.0.0.1:4096"))
        self.cli.registry.register(EngineManifest(name="antigravity", port=58432, web_url="https://localhost:58432"))

    def test_top_level_completions(self):
        candidates = app.complete(self.cli, "zsh", 2, ["agent-sandbox", ""])
        expected_commands = [
            "start",
            "stop",
            "restart",
            "build",
            "status",
            "ui",
            "web",
            "workspace",
            "ws",
            "rules",
            "skills",
            "open",
            "host-bridge",
        ]
        candidate_names = [c.split(":")[0] for c in candidates]
        for cmd in expected_commands:
            self.assertIn(cmd, candidate_names)

    def test_engine_completions_for_start(self):
        candidates = app.complete(self.cli, "zsh", 3, ["agent-sandbox", "start", ""])
        candidate_names = [c.split(":")[0] for c in candidates]
        self.assertIn("opencode", candidate_names)
        self.assertIn("antigravity", candidate_names)
        self.assertIn("--no-host-bridge", candidate_names)
        self.assertIn("--with-host-bridge", candidate_names)

    def test_workspace_subcommand_completions(self):
        candidates = app.complete(self.cli, "zsh", 3, ["agent-sandbox", "workspace", ""])
        candidate_names = [c.split(":")[0] for c in candidates]
        self.assertIn("add", candidate_names)
        self.assertIn("remove", candidate_names)
        self.assertIn("rm", candidate_names)
        self.assertIn("list", candidate_names)
        self.assertIn("ls", candidate_names)

    def test_workspace_remove_dynamic_completions(self):
        with mock.patch.object(self.cli, "ensure_scaffolding") as mock_scaffold:
            from agent_sandbox.config.models import SandboxConfig
            mock_scaffold.return_value = SandboxConfig(
                allowed_workspaces=("/Users/dev/project-a", "/Users/dev/project-b"),
                allowed_commands={},
            )
            candidates = app.complete(self.cli, "zsh", 4, ["agent-sandbox", "workspace", "remove", ""])
            self.assertIn("/Users/dev/project-a", candidates)
            self.assertIn("/Users/dev/project-b", candidates)

    def test_host_bridge_subcommand_completions(self):
        candidates = app.complete(self.cli, "zsh", 3, ["agent-sandbox", "host-bridge", ""])
        candidate_names = [c.split(":")[0] for c in candidates]
        self.assertIn("start", candidate_names)
        self.assertIn("stop", candidate_names)
        self.assertIn("restart", candidate_names)
        self.assertIn("status", candidate_names)
        self.assertIn("fg", candidate_names)
        self.assertIn("run", candidate_names)

    def test_fast_path_main_complete(self):
        import io
        from contextlib import redirect_stdout

        f = io.StringIO()
        with redirect_stdout(f):
            code = main(["_complete", "zsh", "2", "agent-sandbox", ""])
        self.assertEqual(code, 0)
        output = f.getvalue()
        self.assertIn("start:", output)
        self.assertIn("workspace:", output)


if __name__ == "__main__":
    unittest.main()
