"""Typed model and loader for the engine-agnostic compose fragment (config/compose.common.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping

import yaml

COMPOSE_COMMON_REL = os.path.join("config", "compose.common.yaml")
COMMON_KEY = "x-sandbox-common"


@dataclass(frozen=True)
class ComposeBuildConfig:
    context: str
    dockerfile: str
    args: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ComposeResourceConfig:
    cpus: str
    memory: str
    pids: int


@dataclass(frozen=True)
class ComposeCommonConfig:
    image: str
    build: ComposeBuildConfig
    restart: str
    shm_size: str
    cap_drop: tuple[str, ...]
    cap_add: tuple[str, ...]
    resources: ComposeResourceConfig
    extra_hosts: tuple[str, ...]
    ports: tuple[str, ...]
    environment: tuple[str, ...]

    def as_service_fragment(self, build_context: str) -> dict[str, Any]:
        """Returns a plain dict for YAML serialization, with the build context resolved.

        The generated override lives outside the repository (~/.agent-sandbox), so the
        build context must be pinned to an absolute path by the caller.
        """
        build: dict[str, Any] = {
            "context": build_context,
            "dockerfile": self.build.dockerfile,
        }
        if self.build.args:
            build["args"] = self.build.args

        return {
            "image": self.image,
            "build": build,
            "restart": self.restart,
            "shm_size": self.shm_size,
            "cap_drop": list(self.cap_drop),
            "cap_add": list(self.cap_add),
            "deploy": {
                "resources": {
                    "limits": {
                        "cpus": self.resources.cpus,
                        "memory": self.resources.memory,
                        "pids": self.resources.pids,
                    }
                }
            },
            "extra_hosts": list(self.extra_hosts),
            "ports": list(self.ports),
            "environment": list(self.environment),
        }


def _require(data: Mapping[str, Any], key: str, path: str) -> Any:
    """Returns data[key], raising if the key is absent (fail fast on missing config)."""
    if key not in data:
        raise ValueError(f"{path} is missing required key '{key}'.")
    return data[key]


def _require_str(data: Mapping[str, Any], key: str, path: str) -> str:
    value = _require(data, key, path)
    if not isinstance(value, str):
        raise ValueError(f"{path}.{key} must be a string, got {type(value).__name__}.")
    return value


def _require_str_list(data: Mapping[str, Any], key: str, path: str) -> tuple[str, ...]:
    value = _require(data, key, path)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{path}.{key} must be a list of strings.")
    return tuple(value)


def _require_str_map(data: Mapping[str, Any], key: str, path: str) -> dict[str, str]:
    value = _require(data, key, path)
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"{path}.{key} must be a mapping of string -> string.")
    return dict(value)


def _parse_build(data: Mapping[str, Any], path: str) -> ComposeBuildConfig:
    build = _require(data, "build", path)
    if not isinstance(build, dict):
        raise ValueError(f"{path}.build must be a mapping.")
    build_path = f"{path}.build"
    return ComposeBuildConfig(
        context=_require_str(build, "context", build_path),
        dockerfile=_require_str(build, "dockerfile", build_path),
        args=_require_str_map(build, "args", build_path) if "args" in build else {},
    )


def _parse_resources(data: Mapping[str, Any], path: str) -> ComposeResourceConfig:
    deploy = _require(data, "deploy", path)
    if not isinstance(deploy, dict):
        raise ValueError(f"{path}.deploy must be a mapping.")
    resources = _require(deploy, "resources", f"{path}.deploy")
    if not isinstance(resources, dict):
        raise ValueError(f"{path}.deploy.resources must be a mapping.")
    limits = _require(resources, "limits", f"{path}.deploy.resources")
    if not isinstance(limits, dict):
        raise ValueError(f"{path}.deploy.resources.limits must be a mapping.")
    limits_path = f"{path}.deploy.resources.limits"

    pids = _require(limits, "pids", limits_path)
    if not isinstance(pids, int) or isinstance(pids, bool):
        raise ValueError(f"{limits_path}.pids must be an integer.")

    return ComposeResourceConfig(
        cpus=_require_str(limits, "cpus", limits_path),
        memory=_require_str(limits, "memory", limits_path),
        pids=pids,
    )


def parse_compose_common(data: Mapping[str, Any], source: str) -> ComposeCommonConfig:
    """Parses a loaded compose-common document into a ComposeCommonConfig, failing fast on missing fields."""
    if not isinstance(data, dict):
        raise ValueError(f"{source} must be a YAML mapping.")

    fragment = _require(data, COMMON_KEY, source)
    if not isinstance(fragment, dict):
        raise ValueError(f"{source}.{COMMON_KEY} must be a mapping.")

    return ComposeCommonConfig(
        image=_require_str(fragment, "image", COMMON_KEY),
        build=_parse_build(fragment, COMMON_KEY),
        restart=_require_str(fragment, "restart", COMMON_KEY),
        shm_size=_require_str(fragment, "shm_size", COMMON_KEY),
        cap_drop=_require_str_list(fragment, "cap_drop", COMMON_KEY),
        cap_add=_require_str_list(fragment, "cap_add", COMMON_KEY),
        resources=_parse_resources(fragment, COMMON_KEY),
        extra_hosts=_require_str_list(fragment, "extra_hosts", COMMON_KEY),
        ports=_require_str_list(fragment, "ports", COMMON_KEY),
        environment=_require_str_list(fragment, "environment", COMMON_KEY),
    )


def load_compose_common(repo_root: str) -> ComposeCommonConfig:
    """Loads and parses the engine-agnostic compose fragment from config/compose.common.yaml."""
    common_path = os.path.join(repo_root, COMPOSE_COMMON_REL)
    if not os.path.isfile(common_path):
        raise FileNotFoundError(
            f"Engine-agnostic compose template not found at {common_path}. "
            f"It must define a '{COMMON_KEY}' mapping."
        )
    with open(common_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return parse_compose_common(data, source=common_path)
