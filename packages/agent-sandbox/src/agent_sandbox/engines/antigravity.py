"""Antigravity engine adapter. Owns the antigravity user-config shape (empty today)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .base import EngineAdapter
from ..compose.volumes import migrate_antigravity_volume_if_needed


@dataclass(frozen=True)
class AntigravityUserConfig:
    pass


CONFIG_FILENAME = "config.yaml"


def parse_antigravity_user_config(path: Path) -> AntigravityUserConfig:
    """Parses ~/.agent-sandbox/antigravity/config.yaml. No fields supported yet."""
    if not path.is_file():
        return AntigravityUserConfig()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return AntigravityUserConfig()
    if not isinstance(data, dict):
        raise ValueError(f"Invalid antigravity config {path}: top level must be a mapping.")
    if data:
        raise ValueError(
            f"Invalid antigravity config {path}: unexpected keys: {sorted(data.keys())} "
            "(no fields supported yet)"
        )
    return AntigravityUserConfig()


class AntigravityAdapter(EngineAdapter):
    name = "antigravity"

    def default_config(self) -> AntigravityUserConfig:
        return AntigravityUserConfig()

    def parse_user_config(self, path: Path) -> AntigravityUserConfig:
        return parse_antigravity_user_config(path)

    def config_path(self, sandbox_dir: Path) -> Path:
        return sandbox_dir / self.name / CONFIG_FILENAME

    def scaffold(self, sandbox_dir: Path, repo_root: Path) -> None:
        # Reserve the per-engine dir; no config file needed until fields exist.
        # Still ensure rules/skills dirs exist via generic scaffolding in cli.py.
        return None

    def compose_env(self, config: object) -> tuple[str, ...]:
        if not isinstance(config, AntigravityUserConfig):
            raise TypeError(
                f"AntigravityAdapter.compose_env expects AntigravityUserConfig, got {type(config).__name__}"
            )
        return ()

    def status_lines(self, config: object) -> tuple[str, ...]:
        if not isinstance(config, AntigravityUserConfig):
            raise TypeError(
                f"AntigravityAdapter.status_lines expects AntigravityUserConfig, got {type(config).__name__}"
            )
        return ()

    def legacy_container_names(self) -> tuple[str, ...]:
        return ("antigravity-sandbox",)

    def run_legacy_migrations(self) -> None:
        migrate_antigravity_volume_if_needed()
