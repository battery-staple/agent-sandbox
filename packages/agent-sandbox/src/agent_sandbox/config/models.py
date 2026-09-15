"""Immutable data models for engine manifests and sandbox configurations."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class EngineRuleConfig:
    target_file: str
    host_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineSkillConfig:
    target_dir: str
    catalogs: tuple[str, ...] = ()


@dataclass(frozen=True)
class EngineMountConfig:
    host_path: str
    container_path: str
    read_only: bool = False


@dataclass(frozen=True)
class EngineManifest:
    name: str
    port: int
    web_url: str
    rules: EngineRuleConfig | None = None
    skills: EngineSkillConfig | None = None
    mounts: tuple[str, ...] = ()
    directory: str = ""


@dataclass(frozen=True)
class SandboxConfig:
    allowed_workspaces: tuple[str, ...] = ()
    allowed_commands: dict[str, Any] = field(default_factory=dict)
