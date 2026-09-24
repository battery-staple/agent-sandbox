"""Tests for compose override generator in agent_sandbox.compose.generator."""
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
import yaml

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.compose.generator import generate_compose_override
from agent_sandbox.config.models import (
    EngineManifest,
    EngineRuleConfig,
    EngineSkillConfig,
    SandboxConfig,
)


class TestGenerator(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="test_generator_")
        self.fs_root = Path(self.temp_dir) / "fake_home"
        self.fs_root.mkdir(parents=True, exist_ok=True)
        self.repo_root = os.path.join(self.temp_dir, "repo")
        self.sandbox_dir = os.path.join(self.temp_dir, "sandbox")
        os.makedirs(os.path.join(self.repo_root, "customizations", "rules"), exist_ok=True)
        os.makedirs(os.path.join(self.repo_root, "config"), exist_ok=True)
        os.makedirs(os.path.join(self.sandbox_dir, "common", "skills"), exist_ok=True)

        common_fragment = {
            "x-sandbox-common": {
                "image": "agent-sandbox:latest",
                "build": {
                    "context": ".",
                    "dockerfile": "Dockerfile.sandbox",
                    "args": {"USER_UID": "${HOST_UID:-1000}", "USER_GID": "${HOST_GID:-1000}"},
                },
                "restart": "unless-stopped",
                "shm_size": "2gb",
                "cap_drop": ["ALL"],
                "cap_add": ["CHOWN", "SETUID"],
                "deploy": {"resources": {"limits": {"cpus": "4.0", "memory": "8GB", "pids": 1024}}},
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "ports": ["127.0.0.1:3000-3005:3000-3005", "127.0.0.1:5173:5173"],
                "environment": ["HOST_EXEC_HOST=host.docker.internal", "HOST_EXEC_PORT=58433"],
            }
        }
        with open(os.path.join(self.repo_root, "config", "compose.common.yaml"), "w", encoding="utf-8") as f:
            yaml.safe_dump(common_fragment, f, sort_keys=False)

        self.ws_dir = os.path.join(self.temp_dir, "workspace1")
        os.makedirs(self.ws_dir)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_generate_compose_override(self):
        engine_opencode = EngineManifest(
            name="opencode",
            port=4096,
            web_url="http://127.0.0.1:4096",
            rules=EngineRuleConfig(target_file="~/.config/opencode/AGENTS.md"),
            skills=EngineSkillConfig(target_dir="~/.config/opencode/skills"),
            mounts=("~/.config/opencode",),
        )

        engine_anti = EngineManifest(
            name="antigravity",
            port=58432,
            web_url="https://localhost:58432",
            rules=EngineRuleConfig(target_file="~/.gemini/GEMINI.md"),
            skills=EngineSkillConfig(target_dir="/home/developer/.gemini/antigravity/builtin/skills"),
            mounts=("~/.gemini",),
        )

        config = SandboxConfig(
            allowed_workspaces=(self.ws_dir,),
        )

        override_file = os.path.join(self.temp_dir, "docker-compose.override.yml")
        yaml_out = generate_compose_override(
            active_engines=[engine_opencode, engine_anti],
            config=config,
            repo_root=self.repo_root,
            sandbox_dir=self.sandbox_dir,
            override_file=override_file,
            fs_root=self.fs_root,
        )

        self.assertTrue(os.path.isfile(override_file))
        parsed = yaml.safe_load(yaml_out)
        self.assertIn("services", parsed)
        self.assertIn("opencode", parsed["services"])
        self.assertIn("antigravity", parsed["services"])

        # Common fragment merged into every service
        for svc in parsed["services"].values():
            self.assertEqual(svc["image"], "agent-sandbox:latest")
            self.assertEqual(svc["restart"], "unless-stopped")
            self.assertEqual(svc["shm_size"], "2gb")
            self.assertEqual(svc["cap_drop"], ["ALL"])
            self.assertEqual(svc["build"]["dockerfile"], "Dockerfile.sandbox")
            self.assertEqual(svc["build"]["context"], os.path.abspath(self.repo_root))
            self.assertIn("127.0.0.1:3000-3005:3000-3005", svc["ports"])
            self.assertTrue(any(env.startswith("HOST_EXEC_HOST=") for env in svc["environment"]))

        # Validate opencode service
        opencode_svc = parsed["services"]["opencode"]
        self.assertEqual(opencode_svc["container_name"], "agent-sandbox-opencode")
        self.assertIn("127.0.0.1:4096:4096", opencode_svc["ports"])
        self.assertIn("ENGINE=opencode", opencode_svc["environment"])
        self.assertIn("SANDBOX_SKILLS_TARGET=/home/developer/.config/opencode/skills", opencode_svc["environment"])
        volumes = opencode_svc["volumes"]
        # Home volume + IPC secret mount
        self.assertIn("agent_home_opencode:/home/developer", volumes)
        self.assertTrue(any(v.endswith(":/var/run/host-exec:ro") for v in volumes))
        # Workspace
        self.assertTrue(any(v.startswith(f"{self.ws_dir}:{self.ws_dir}:cached") for v in volumes))
        # Rules target
        self.assertTrue(any(v.endswith(":/home/developer/.config/opencode/AGENTS.md") for v in volumes))
        # Ensure host target file was touched/created under the injected fs_root
        host_agents_target = self.fs_root / ".config" / "opencode" / "AGENTS.md"
        self.assertTrue(host_agents_target.is_file())
        # Mounts
        self.assertTrue(any(v.endswith(":/home/developer/.config/opencode") for v in volumes))
        self.assertTrue((self.fs_root / ".config" / "opencode").is_dir())
        self.assertTrue((self.fs_root / ".gemini").is_dir())

        # Validate antigravity service
        anti_svc = parsed["services"]["antigravity"]
        self.assertEqual(anti_svc["container_name"], "agent-sandbox-antigravity")
        self.assertIn("127.0.0.1:58432:58432", anti_svc["ports"])
        self.assertIn("ENGINE=antigravity", anti_svc["environment"])
        anti_volumes = anti_svc["volumes"]
        self.assertIn("agent_home_antigravity:/home/developer", anti_volumes)
        self.assertTrue(any(v.endswith(":/home/developer/.gemini/GEMINI.md") for v in anti_volumes))
        self.assertTrue(any(v.endswith(":/home/developer/.gemini") for v in anti_volumes))

        # Top-level convention volumes
        self.assertEqual(parsed["volumes"]["agent_home_opencode"]["name"], "agent_home_opencode")
        self.assertEqual(parsed["volumes"]["agent_home_antigravity"]["name"], "agent_home_antigravity")

    def test_generate_compose_override_preserves_existing_services(self):
        engine_anti = EngineManifest(
            name="antigravity",
            port=58432,
            web_url="https://localhost:58432",
            rules=EngineRuleConfig(target_file="~/.gemini/GEMINI.md"),
            skills=EngineSkillConfig(target_dir="/home/developer/.gemini/antigravity/builtin/skills"),
            mounts=("~/.gemini",),
        )
        engine_opencode = EngineManifest(
            name="opencode",
            port=4096,
            web_url="http://127.0.0.1:4096",
            rules=EngineRuleConfig(target_file="~/.config/opencode/AGENTS.md"),
            skills=EngineSkillConfig(target_dir="~/.config/opencode/skills"),
            mounts=("~/.config/opencode",),
        )
        config = SandboxConfig(allowed_workspaces=(self.ws_dir,))
        override_file = os.path.join(self.temp_dir, "docker-compose.override.yml")

        # First run: start antigravity only
        generate_compose_override(
            active_engines=[engine_anti],
            config=config,
            repo_root=self.repo_root,
            sandbox_dir=self.sandbox_dir,
            override_file=override_file,
            fs_root=self.fs_root,
        )
        with open(override_file, "r", encoding="utf-8") as f:
            first_parsed = yaml.safe_load(f)
        self.assertIn("antigravity", first_parsed["services"])
        self.assertNotIn("opencode", first_parsed["services"])
        self.assertIn("agent_home_antigravity", first_parsed["volumes"])

        # Second run: start opencode only - must preserve antigravity!
        generate_compose_override(
            active_engines=[engine_opencode],
            config=config,
            repo_root=self.repo_root,
            sandbox_dir=self.sandbox_dir,
            override_file=override_file,
            fs_root=self.fs_root,
        )
        with open(override_file, "r", encoding="utf-8") as f:
            second_parsed = yaml.safe_load(f)
        self.assertIn("antigravity", second_parsed["services"])
        self.assertIn("opencode", second_parsed["services"])
        self.assertIn("agent_home_antigravity", second_parsed["volumes"])
        self.assertIn("agent_home_opencode", second_parsed["volumes"])


if __name__ == "__main__":
    unittest.main()
