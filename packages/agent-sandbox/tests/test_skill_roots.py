"""Tests for the skill-root CLI command group and rejection reporting."""
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.cli import SandboxCLI, main
from agent_sandbox.config.loader import save_sandbox_config
from agent_sandbox.config.models import EngineManifest, EngineSkillConfig, SandboxConfig


def _cli(tmpdir: str) -> SandboxCLI:
    sandbox_dir = os.path.join(tmpdir, "sandbox")
    os.makedirs(sandbox_dir, exist_ok=True)
    example_dir = os.path.join(tmpdir, "engines", "opencode")
    os.makedirs(example_dir, exist_ok=True)
    example = os.path.join(example_dir, "config.example.yaml")
    if not os.path.isfile(example):
        with open(example, "w") as f:
            f.write("server:\n  username: opencode\n  password: null\n")
    cli = SandboxCLI(repo_root=tmpdir, sandbox_dir=sandbox_dir)
    cli.registry.register(EngineManifest(name="opencode", port=4096, web_url="http://127.0.0.1:4096"))
    return cli


class TestSkillRootCommands(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_skill_roots_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_add_persists_and_lists(self):
        cli = _cli(self.tmpdir)
        root = os.path.join(self.tmpdir, "shared-skills")
        os.makedirs(root)
        with mock.patch.object(cli, "get_running_engines", return_value=()):
            self.assertEqual(cli.cmd_skill_root_add(root), 0)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_skill_root_list(), 0)
        self.assertIn(os.path.abspath(root), out.getvalue())

    def test_add_missing_dir_requires_force(self):
        cli = _cli(self.tmpdir)
        missing = os.path.join(self.tmpdir, "nope")
        with mock.patch.object(cli, "get_running_engines", return_value=()):
            self.assertEqual(cli.cmd_skill_root_add(missing), 1)
            self.assertEqual(cli.cmd_skill_root_add(missing, force=True), 0)
        self.assertTrue(os.path.isdir(missing))

    def test_add_duplicate_is_noop(self):
        cli = _cli(self.tmpdir)
        root = os.path.join(self.tmpdir, "shared-skills")
        os.makedirs(root)
        with mock.patch.object(cli, "get_running_engines", return_value=()):
            self.assertEqual(cli.cmd_skill_root_add(root), 0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.cmd_skill_root_add(root), 0)
        self.assertIn("already allowed", out.getvalue())

    def test_remove(self):
        cli = _cli(self.tmpdir)
        root = os.path.join(self.tmpdir, "shared-skills")
        os.makedirs(root)
        with mock.patch.object(cli, "get_running_engines", return_value=()):
            cli.cmd_skill_root_add(root)
            self.assertEqual(cli.cmd_skill_root_remove(root), 0)
            with contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.cmd_skill_root_remove(root), 0)
        self.assertIn("not allowed", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()) as out:
            cli.cmd_skill_root_list()
        self.assertNotIn(os.path.abspath(root), out.getvalue())

    def test_add_restarts_running_engines(self):
        cli = _cli(self.tmpdir)
        root = os.path.join(self.tmpdir, "shared-skills")
        os.makedirs(root)
        running = (cli.registry.get("opencode"),)
        with (
            mock.patch.object(cli, "get_running_engines", return_value=running),
            mock.patch.object(cli, "cmd_start", return_value=0) as start,
        ):
            cli.cmd_skill_root_add(root)
        start.assert_called_once_with(["opencode"])

    def test_status_shows_skill_roots(self):
        cli = _cli(self.tmpdir)
        root = os.path.join(self.tmpdir, "shared-skills")
        os.makedirs(root)
        save_sandbox_config(
            SandboxConfig(allowed_skill_roots=(os.path.abspath(root),)), cli.config_file
        )
        with (
            mock.patch.object(cli, "get_running_engines", return_value=()),
            contextlib.redirect_stdout(io.StringIO()) as out,
        ):
            self.assertEqual(cli.cmd_status(), 0)
        self.assertIn("Allowed Skill Roots", out.getvalue())
        self.assertIn(os.path.abspath(root), out.getvalue())


class TestSkillsRejectionOutput(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_skills_output_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _cli_with_catalog(self) -> SandboxCLI:
        sandbox_dir = os.path.join(self.tmpdir, "sandbox")
        os.makedirs(sandbox_dir, exist_ok=True)
        example_dir = os.path.join(self.tmpdir, "engines", "opencode")
        os.makedirs(example_dir, exist_ok=True)
        with open(os.path.join(example_dir, "config.example.yaml"), "w") as f:
            f.write("server:\n  username: opencode\n  password: null\n")
        cli = SandboxCLI(repo_root=self.tmpdir, sandbox_dir=sandbox_dir)
        cli.registry.register(
            EngineManifest(
                name="opencode",
                port=4096,
                web_url="http://127.0.0.1:4096",
                skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
            )
        )
        ws_dir = os.path.join(self.tmpdir, "workspace")
        os.makedirs(ws_dir)
        sensitive = os.path.join(self.tmpdir, "sensitive")
        os.makedirs(sensitive)
        with open(os.path.join(ws_dir, "skills.json"), "w") as f:
            json.dump({"entries": [{"path": sensitive}]}, f)
        save_sandbox_config(SandboxConfig(allowed_workspaces=(ws_dir,)), cli.config_file)
        return cli

    def test_skills_lists_rejection_with_remedy(self):
        cli = self._cli_with_catalog()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_skills(None), 0)
        text = out.getvalue()
        self.assertIn("Rejected Catalog Skills", text)
        self.assertIn("sensitive", text)
        self.assertIn("skills.json", text)
        self.assertIn("agent-sandbox skill-root add", text)

    def test_skills_no_rejection_section_when_clean(self):
        cli = self._cli_with_catalog()
        root = os.path.join(self.tmpdir, "sensitive")
        save_sandbox_config(
            SandboxConfig(
                allowed_workspaces=(os.path.join(self.tmpdir, "workspace"),),
                allowed_skill_roots=(root,),
            ),
            cli.config_file,
        )
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.cmd_skills(None), 0)
        self.assertNotIn("Rejected Catalog Skills", out.getvalue())


class TestSkillRootDispatcher(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_skill_root_dispatch_")
        self.env_patcher = mock.patch.dict(os.environ, {"AGENT_SANDBOX_REPO_ROOT": self.tmpdir})
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_main_dispatches_add_list_remove(self):
        with mock.patch.object(SandboxCLI, "cmd_skill_root_add", return_value=0) as m:
            self.assertEqual(main(["skill-root", "add", "/tmp/x"]), 0)
        m.assert_called_once()
        with mock.patch.object(SandboxCLI, "cmd_skill_root_list", return_value=0) as m:
            self.assertEqual(main(["skill-root", "list"]), 0)
        m.assert_called_once()
        with mock.patch.object(SandboxCLI, "cmd_skill_root_remove", return_value=0) as m:
            self.assertEqual(main(["skill-root", "remove", "/tmp/x"]), 0)
        m.assert_called_once()

    def test_main_unknown_subcommand_fails(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["skill-root", "bogus"]), 1)

    def test_help_mentions_skill_root(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("skill-root", out.getvalue())


if __name__ == "__main__":
    unittest.main()
