"""Tests for DirectorySkillResolver and CatalogSkillResolver."""
import json
import os
import shutil
import sys
import tempfile
import unittest

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.config.models import EngineManifest, EngineSkillConfig
from agent_sandbox.skills.catalog import CatalogSkillResolver
from agent_sandbox.skills.directory import DirectorySkillResolver


class TestSkills(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_skills_")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_directory_skill_resolver(self):
        sandbox_dir = os.path.join(self.temp_dir, "sandbox")
        common_skill = os.path.join(sandbox_dir, "common", "skills", "git-helper")
        engine_override = os.path.join(sandbox_dir, "opencode", "skills", "git-helper")
        engine_unique = os.path.join(sandbox_dir, "opencode", "skills", "opencode-tool")
        no_skill = os.path.join(sandbox_dir, "common", "skills", "not-a-skill")

        os.makedirs(common_skill)
        with open(os.path.join(common_skill, "SKILL.md"), "w") as f:
            f.write("# Common Git Helper")

        os.makedirs(engine_override)
        with open(os.path.join(engine_override, "SKILL.md"), "w") as f:
            f.write("# OpenCode Git Helper")

        os.makedirs(engine_unique)
        with open(os.path.join(engine_unique, "SKILL.md"), "w") as f:
            f.write("# OpenCode Tool")

        os.makedirs(no_skill)  # No SKILL.md

        resolver = DirectorySkillResolver(sandbox_dir=sandbox_dir)
        skills = resolver.resolve_skills_for_engine("opencode")

        self.assertEqual(len(skills), 2)
        self.assertIn("git-helper", skills)
        self.assertIn("opencode-tool", skills)
        self.assertNotIn("not-a-skill", skills)
        # Verify engine override
        self.assertEqual(skills["git-helper"], os.path.abspath(engine_override))

    def test_catalog_skill_resolver_inheritance_and_conflicts(self):
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        # Skill outside workspace
        ext_skill = os.path.join(self.temp_dir, "external_skills", "skill_ext")
        os.makedirs(ext_skill)

        # Skill inside workspace
        int_skill = os.path.join(ws_dir, "internal_skill")
        os.makedirs(int_skill)

        catalog_path = os.path.join(ws_dir, "skills.json")
        with open(catalog_path, "w") as f:
            json.dump({
                "entries": [
                    {"path": ext_skill},
                    {"path": int_skill},
                ]
            }, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(
            allowed_workspaces=[ws_dir],
            allowed_skill_roots=[os.path.join(self.temp_dir, "external_skills")],
        )
        resolved = resolver.resolve_for_engine(engine, warn=False)

        self.assertEqual(len(resolved), 1)
        self.assertIn(os.path.abspath(ext_skill), resolved)
        self.assertNotIn(os.path.abspath(int_skill), resolved)

    def test_catalog_resolver_rejects_paths_outside_allowed_roots(self):
        """An agent-writable catalog must not mount arbitrary host directories."""
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        sensitive = os.path.join(self.temp_dir, "sensitive", ".ssh")
        os.makedirs(sensitive)

        catalog_path = os.path.join(ws_dir, "skills.json")
        with open(catalog_path, "w") as f:
            json.dump({"entries": [{"path": sensitive}]}, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(allowed_workspaces=[ws_dir])
        resolved = resolver.resolve_for_engine(engine, warn=False)
        self.assertEqual(resolved, [])

    def test_catalog_resolver_allows_configured_skill_root(self):
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        root = os.path.join(self.temp_dir, "shared-skills")
        skill = os.path.join(root, "team-skill")
        os.makedirs(skill)

        catalog_path = os.path.join(ws_dir, "skills.json")
        with open(catalog_path, "w") as f:
            json.dump({"entries": [{"path": skill}]}, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(allowed_workspaces=[ws_dir], allowed_skill_roots=[root])
        resolved = resolver.resolve_for_engine(engine, warn=False)
        self.assertIn(os.path.abspath(skill), resolved)

    def test_catalog_resolver_allows_sandbox_dir(self):
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        sandbox_dir = os.path.join(self.temp_dir, "sandbox")
        skill = os.path.join(sandbox_dir, "common", "skills", "helper")
        os.makedirs(skill)
        # A non-skills sandbox subtree (e.g. logs) must not be trusted.
        logs = os.path.join(sandbox_dir, "logs")
        os.makedirs(logs)

        catalog_path = os.path.join(ws_dir, "skills.json")
        with open(catalog_path, "w") as f:
            json.dump({"entries": [{"path": skill}, {"path": logs}]}, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(allowed_workspaces=[ws_dir], sandbox_dir=sandbox_dir)
        resolved = resolver.resolve_for_engine(engine, warn=False)
        self.assertIn(os.path.abspath(skill), resolved)
        self.assertNotIn(os.path.abspath(logs), resolved)

    def test_catalog_resolver_canonicalizes_and_rejects_symlinks(self):
        """Symlinks cannot redirect a mount away from a trusted root (TOCTOU)."""
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        trusted = os.path.join(self.temp_dir, "trusted")
        trusted_skill = os.path.join(trusted, "real-skill")
        os.makedirs(trusted_skill)

        sensitive = os.path.join(self.temp_dir, "sensitive")
        os.makedirs(sensitive)

        # Agent-writable directory outside the workspace and outside trusted roots.
        agentrw = os.path.join(self.temp_dir, "agentrw")
        os.makedirs(agentrw)
        link_to_trusted = os.path.join(agentrw, "good")
        link_to_sensitive = os.path.join(agentrw, "evil")
        os.symlink(trusted_skill, link_to_trusted)
        os.symlink(sensitive, link_to_sensitive)

        catalog_path = os.path.join(ws_dir, "skills.json")
        with open(catalog_path, "w") as f:
            json.dump({"entries": [{"path": link_to_trusted}, {"path": link_to_sensitive}]}, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(allowed_workspaces=[ws_dir], allowed_skill_roots=[trusted])
        resolved = resolver.resolve_for_engine(engine, warn=False)

        # Only the canonical trusted path is returned, never the symlink itself.
        self.assertEqual(resolved, [os.path.realpath(trusted_skill)])
        self.assertNotIn(os.path.abspath(link_to_trusted), resolved)
        self.assertNotIn(os.path.abspath(link_to_sensitive), resolved)


if __name__ == "__main__":
    unittest.main()
