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
from agent_sandbox.config.models import EngineConnectionConfig, EngineManifest
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

    def test_unrecognized_option_fails_fast(self):
        test_app = CLIApp(name="test-cli")

        @test_app.command("run")
        def cmd_run():
            return 0

        code = test_app.dispatch(None, ["run", "--unknown-flag"])
        self.assertEqual(code, 1)

    def test_option_requires_argument(self):
        test_app = CLIApp(name="test-cli")

        @test_app.command("build")
        @option("--tag", is_flag=False, dest="tag")
        def cmd_build(tag: str = "latest"):
            return 0

        code = test_app.dispatch(None, ["build", "--tag"])
        self.assertEqual(code, 1)

    def test_end_of_options_delimiter(self):
        test_app = CLIApp(name="test-cli")
        received = []

        @test_app.command("echo")
        @argument("args", nargs="*")
        def cmd_echo(args: list[str]):
            received.extend(args)
            return 0

        code = test_app.dispatch(None, ["echo", "--", "--not-a-flag", "hello"])
        self.assertEqual(code, 0)
        self.assertEqual(received, ["--not-a-flag", "hello"])

    def test_pass_context_standalone(self):
        test_app = CLIApp(name="test-cli")
        captured_ctx = []

        @test_app.command("ctx-cmd", pass_context=True)
        def cmd_with_ctx(ctx):
            captured_ctx.append(ctx)
            return 0

        code = test_app.dispatch("my-context-object", ["ctx-cmd"])
        self.assertEqual(code, 0)
        self.assertEqual(captured_ctx, ["my-context-object"])

    def test_strict_exit_code_conversion(self):
        test_app = CLIApp(name="test-cli")

        @test_app.command("none-cmd")
        def cmd_none():
            return None

        @test_app.command("bool-cmd")
        def cmd_bool():
            return True

        @test_app.command("invalid-cmd")
        def cmd_invalid():
            return "not-an-int"

        self.assertEqual(test_app.dispatch(None, ["none-cmd"]), 0)
        self.assertEqual(test_app.dispatch(None, ["bool-cmd"]), 0)
        with self.assertRaises(TypeError):
            test_app.dispatch(None, ["invalid-cmd"])

    def test_bounds_checked_completion(self):
        test_app = CLIApp(name="test-cli")
        grp = test_app.group("ws")

        @grp.command("list")
        def ws_list():
            pass

        # Incomplete or short words list at level 2+ shouldn't raise IndexError
        self.assertEqual(test_app.complete(None, "bash", 2, ["test-cli"]), [])
        self.assertEqual(test_app.complete(None, "bash", 3, ["test-cli", "ws"]), [])


class TestSandboxCLICompletions(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_framework_cli_")
        self.env_patcher = mock.patch.dict(os.environ, {"AGENT_SANDBOX_REPO_ROOT": self.tmpdir})
        self.env_patcher.start()
        self.cli = SandboxCLI(repo_root=self.tmpdir, sandbox_dir=os.path.join(self.tmpdir, "sandbox"))
        self.cli.registry.register(EngineManifest(name="opencode", port=4096, connection=EngineConnectionConfig(type="web", url="http://127.0.0.1:4096")))
        self.cli.registry.register(EngineManifest(name="antigravity", port=58432, connection=EngineConnectionConfig(type="web", url="https://localhost:58432")))

    def tearDown(self):
        self.env_patcher.stop()
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

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
        from agent_sandbox.config.models import SandboxConfig
        from agent_sandbox.config.loader import save_sandbox_config

        config = SandboxConfig(
            allowed_workspaces=("/Users/dev/project-a", "/Users/dev/project-b"),
            allowed_commands={},
        )
        save_sandbox_config(config, self.cli.config_file)
        with mock.patch.object(self.cli, "ensure_scaffolding") as mock_scaffold:
            candidates = app.complete(self.cli, "zsh", 4, ["agent-sandbox", "workspace", "remove", ""])
            self.assertIn("/Users/dev/project-a", candidates)
            self.assertIn("/Users/dev/project-b", candidates)
            # Verify tab completion never triggers mutating scaffolding or migrations
            self.assertFalse(mock_scaffold.called)

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
