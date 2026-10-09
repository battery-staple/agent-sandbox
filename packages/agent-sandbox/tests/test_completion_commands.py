"""Tests for shell completion generators and profile installer."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.cli import SandboxCLI, main
from agent_sandbox.framework import generate_completion_script


class TestCompletionCommands(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_completion_")
        self.cli = SandboxCLI(repo_root=self.tmpdir, sandbox_dir=os.path.join(self.tmpdir, "sandbox"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_generate_zsh_script(self):
        script = generate_completion_script("zsh", "agent-sandbox")
        self.assertIn("#compdef agent-sandbox", script)
        self.assertIn("compdef _agent_sandbox agent-sandbox", script)
        self.assertIn("agent-sandbox _complete zsh", script)
        self.assertIn("_describe -t commands 'agent-sandbox' completions", script)

    def test_generate_bash_script(self):
        script = generate_completion_script("bash", "agent-sandbox")
        self.assertIn("complete -F _agent_sandbox_complete agent-sandbox", script)
        self.assertIn("agent-sandbox _complete bash", script)

    def test_unsupported_shell_raises(self):
        with self.assertRaises(ValueError):
            generate_completion_script("fish", "agent-sandbox")

    def test_main_completion_zsh(self):
        f = io.StringIO()
        with redirect_stdout(f):
            code = main(["completion", "zsh"])
        self.assertEqual(code, 0)
        self.assertIn("#compdef agent-sandbox", f.getvalue())

    def test_main_completion_bash(self):
        f = io.StringIO()
        with redirect_stdout(f):
            code = main(["completion", "bash"])
        self.assertEqual(code, 0)
        self.assertIn("complete -F _agent_sandbox_complete agent-sandbox", f.getvalue())

    def test_install_zsh_into_rc_file(self):
        fake_home = os.path.join(self.tmpdir, "home")
        os.makedirs(fake_home, exist_ok=True)
        zshrc = os.path.join(fake_home, ".zshrc")

        with mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", fake_home)):
            code = self.cli.cmd_completion_install(shell="zsh")
            self.assertEqual(code, 0)

            with open(zshrc, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn('eval "$(agent-sandbox completion zsh)"', content)

            # Test idempotency (should not add duplicate)
            code = self.cli.cmd_completion_install(shell="zsh")
            self.assertEqual(code, 0)
            with open(zshrc, "r", encoding="utf-8") as f:
                lines = f.readlines()
            eval_count = sum(1 for line in lines if 'eval "$(agent-sandbox completion zsh)"' in line)
            self.assertEqual(eval_count, 1)

    def test_install_bash_into_rc_file(self):
        fake_home = os.path.join(self.tmpdir, "home")
        os.makedirs(fake_home, exist_ok=True)
        bashrc = os.path.join(fake_home, ".bashrc")

        with mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", fake_home)):
            code = self.cli.cmd_completion_install(shell="bash")
            self.assertEqual(code, 0)

            with open(bashrc, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn('eval "$(agent-sandbox completion bash)"', content)

    def test_install_unsupported_shell(self):
        code = self.cli.cmd_completion_install(shell="csh")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
