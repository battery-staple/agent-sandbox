"""Tests for DirectorySkillResolver and CatalogSkillResolver."""
import contextlib
import io
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
        self.temp_dir = os.path.realpath(tempfile.mkdtemp(prefix="test_skills_"))

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
        """Symlink entries resolve to canonical paths; a swapped link cannot redirect the mount."""
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

        # Repoint the good link at the sensitive dir after validation.
        os.remove(link_to_trusted)
        os.symlink(sensitive, link_to_trusted)
        resolved_again = resolver.resolve_for_engine(engine, warn=False)
        self.assertEqual(resolved_again, [])
        # The previously returned path still points inside the trusted root.
        self.assertTrue(resolved[0].startswith(os.path.realpath(trusted) + os.sep))
        self.assertTrue(os.path.isdir(resolved[0]))

    def test_catalog_resolver_rejects_symlink_inside_root_pointing_outside(self):
        """A symlink inside a trusted root that points outside the root is rejected."""
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        trusted = os.path.join(self.temp_dir, "trusted")
        os.makedirs(trusted)

        outside = os.path.join(self.temp_dir, "outside")
        os.makedirs(outside)

        inner_link = os.path.join(trusted, "inner")
        os.symlink(outside, inner_link)

        catalog_path = os.path.join(ws_dir, "skills.json")
        with open(catalog_path, "w") as f:
            json.dump({"entries": [{"path": inner_link}]}, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(allowed_workspaces=[ws_dir], allowed_skill_roots=[trusted])
        resolved = resolver.resolve_for_engine(engine, warn=False)
        self.assertEqual(resolved, [])

    def test_filter_allowed_roots_respects_warn_flag(self):
        outside = os.path.join(self.temp_dir, "outside")
        os.makedirs(outside)

        resolver = CatalogSkillResolver(allowed_workspaces=[])
        with contextlib.redirect_stderr(io.StringIO()) as silent:
            allowed = resolver.filter_allowed_roots([outside], [], warn=False)
        self.assertEqual(allowed, [])
        self.assertEqual(silent.getvalue(), "")
        self.assertEqual(len(resolver.last_rejections), 1)
        rejection = resolver.last_rejections[0]
        self.assertEqual(rejection.path, outside)
        self.assertTrue(rejection.reason)

        with contextlib.redirect_stderr(io.StringIO()) as noisy:
            resolver.filter_allowed_roots([outside], [], warn=True)
        self.assertIn("outside allowed roots", noisy.getvalue())

    def test_resolve_for_engine_warn_false_is_silent(self):
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        sensitive = os.path.join(self.temp_dir, "sensitive")
        os.makedirs(sensitive)

        with open(os.path.join(ws_dir, "skills.json"), "w") as f:
            json.dump({"entries": [{"path": sensitive}]}, f)

        engine = EngineManifest(
            name="test",
            port=1234,
            web_url="http://localhost",
            skills=EngineSkillConfig(target_dir="skills", catalogs=("skills.json",)),
        )

        resolver = CatalogSkillResolver(allowed_workspaces=[ws_dir])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            resolved = resolver.resolve_for_engine(engine, warn=False)
        self.assertEqual(resolved, [])
        self.assertEqual(err.getvalue(), "")

    def test_resolve_with_rejections_names_declaring_catalog(self):
        ws_dir = os.path.join(self.temp_dir, "workspace")
        os.makedirs(ws_dir)

        sensitive = os.path.join(self.temp_dir, "sensitive")
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
        with contextlib.redirect_stderr(io.StringIO()) as err:
            accepted, rejections = resolver.resolve_with_rejections(engine, warn=False)
        self.assertEqual(accepted, [])
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].path, os.path.abspath(sensitive))
        self.assertEqual(rejections[0].source_catalog, os.path.abspath(catalog_path))
        self.assertTrue(rejections[0].reason)


if __name__ == "__main__":
    unittest.main()
