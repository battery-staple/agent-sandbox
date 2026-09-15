"""Directory-based skill resolution for container-first customizations (~/.agent-sandbox)."""
from __future__ import annotations

import os
from typing import Mapping


class DirectorySkillResolver:
    """Discovers skills from filesystem directories (e.g. ~/.agent-sandbox/{common,<engine>}/skills)."""

    def __init__(self, sandbox_dir: str) -> None:
        self.sandbox_dir = os.path.abspath(os.path.expanduser(sandbox_dir))

    def resolve_skills_for_engine(self, engine_name: str) -> dict[str, str]:
        """
        Discovers all directory skills applicable to an engine.
        Engine-specific skills override common skills with the same name.
        Returns a dict mapping skill_name -> host_absolute_path.
        """
        discovered: dict[str, str] = {}

        common_dir = os.path.join(self.sandbox_dir, "common", "skills")
        engine_dir = os.path.join(self.sandbox_dir, engine_name, "skills")

        # 1. Common skills
        if os.path.isdir(common_dir):
            for entry in sorted(os.listdir(common_dir)):
                p = os.path.join(common_dir, entry)
                if os.path.isdir(p) and os.path.isfile(os.path.join(p, "SKILL.md")):
                    discovered[entry] = os.path.abspath(p)

        # 2. Engine-specific skills (override common)
        if os.path.isdir(engine_dir):
            for entry in sorted(os.listdir(engine_dir)):
                p = os.path.join(engine_dir, entry)
                if os.path.isdir(p) and os.path.isfile(os.path.join(p, "SKILL.md")):
                    discovered[entry] = os.path.abspath(p)

        return discovered
