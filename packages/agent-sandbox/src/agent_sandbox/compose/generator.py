"""Docker Compose override generator for active engines, ports, workspaces, and skills."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence
import yaml

from ..config.models import EngineManifest, SandboxConfig
from ..engines import CONTAINER_NAME_PREFIX, EngineRuntime, get_adapter, get_runtime
from ..skills.catalog import CatalogSkillResolver
from ..skills.directory import DirectorySkillResolver
from .common import load_compose_common
from .rules import compile_rules_for_engine

HOME_MOUNT_TARGET = "/home/developer"
IPC_VOLUME_SPEC = "${HOST_IPC_PATH:-${HOME}/.agent-sandbox/ipc}:/var/run/host-exec:ro"


def _to_container_path(path: str) -> str:
    """Converts a home-relative or absolute path into a container developer user path."""
    if path.startswith("~/"):
        return f"{HOME_MOUNT_TARGET}/{path[2:]}"
    if path == "~":
        return HOME_MOUNT_TARGET
    return path


def _resolve_host_path(path: Path, fs_root: Path) -> Path:
    """Resolves a host path against the injected filesystem root."""
    p_str = str(path)
    root = fs_root.resolve()
    if p_str.startswith("~/"):
        return (root / p_str[2:]).resolve()
    if p_str == "~":
        return root
    if not path.is_absolute():
        return (root / path).resolve()
    return path.resolve()


def generate_compose_override(
    active_engines: Sequence[EngineManifest],
    config: SandboxConfig,
    repo_root: str,
    fs_root: Path,
    sandbox_dir: str = "~/.agent-sandbox",
    override_file: str | None = None,
    engine_runtimes: Sequence[EngineRuntime] | None = None,
) -> str:
    """
    Dynamically generates docker-compose.override.yml configuration for all active engines.
    Emits complete per-engine services by merging the engine-agnostic common fragment
    (config/compose.common.yaml) with engine-specific identity, ports, workspaces, catalog
    skill mounts, directory skill mounts, engine config mounts, and compiled rule
    shadow-mounts.
    """
    norm_sandbox = os.path.abspath(os.path.expanduser(sandbox_dir))
    norm_repo = os.path.abspath(repo_root)

    common = load_compose_common(norm_repo)
    common_ports = list(common.ports)
    common_env = list(common.environment)

    dir_skill_resolver = DirectorySkillResolver(sandbox_dir=norm_sandbox)
    catalog_skill_resolver = CatalogSkillResolver(allowed_workspaces=config.allowed_workspaces)

    out_path = os.path.abspath(os.path.expanduser(override_file)) if override_file else None

    services: dict[str, dict] = {}
    volumes_top: dict[str, dict] = {}

    if out_path and os.path.isfile(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                prev_data = yaml.safe_load(f) or {}
            if isinstance(prev_data, dict):
                prev_services = prev_data.get("services")
                if isinstance(prev_services, dict):
                    services.update(prev_services)
                prev_volumes = prev_data.get("volumes")
                if isinstance(prev_volumes, dict):
                    volumes_top.update(prev_volumes)
        except Exception:
            pass

    for engine in active_engines:
        service_entry = common.as_service_fragment(build_context=norm_repo)

        # Engine identity on the Docker host network
        service_entry["container_name"] = f"{CONTAINER_NAME_PREFIX}{engine.name}"
        service_entry["hostname"] = f"{CONTAINER_NAME_PREFIX}{engine.name}"

        # Web port forwards (common dev ports + the engine's own port)
        service_entry["ports"] = common_ports + [f"127.0.0.1:{engine.port}:{engine.port}"]

        # Runtime environment (generic identity + per-engine adapter env, no engine branches)
        env_items = [f"ENGINE={engine.name}"]
        if engine.skills and engine.skills.target_dir:
            env_items.append(f"SANDBOX_SKILLS_TARGET={_to_container_path(engine.skills.target_dir)}")
        if engine_runtimes is None:
            # No parse-once runtimes passed (e.g. older callers/tests): use the
            # adapter default. Unknown engine names raise KeyError here.
            _adapter = get_adapter(engine.name)
            env_items.extend(_adapter.compose_env(_adapter.default_config()))
        else:
            runtime = get_runtime(engine_runtimes, engine.name)
            env_items.extend(runtime.adapter.compose_env(runtime.config))
        env_items.extend(common_env)
        service_entry["environment"] = env_items

        volumes: list[str] = [
            # Persistent user home directory on an engine-isolated volume
            f"agent_home_{engine.name}:{HOME_MOUNT_TARGET}",
            # Host-Exec shared auth secret directory
            IPC_VOLUME_SPEC,
        ]

        # 1. Engine config mounts
        for m in engine.mounts:
            host_m = _resolve_host_path(Path(m), fs_root)
            container_m = _to_container_path(m)
            # Create host directory if it does not exist
            host_m.mkdir(parents=True, exist_ok=True)
            volumes.append(f"{host_m}:{container_m}")

        # 2. Rules shadow-mount
        if engine.rules and engine.rules.target_file:
            rules_res = compile_rules_for_engine(engine, repo_root=norm_repo, sandbox_dir=norm_sandbox)
            container_rules_target = _to_container_path(engine.rules.target_file)
            # Ensure the target file exists on host if it falls within a host-mounted directory,
            # preventing VirtioFS mountpoint creation failure in runc ("outside of rootfs").
            host_rules_target = _resolve_host_path(Path(engine.rules.target_file), fs_root)
            host_rules_target.parent.mkdir(parents=True, exist_ok=True)
            host_rules_target.touch(exist_ok=True)
            volumes.append(f"{rules_res.host_compiled_file}:{container_rules_target}")

        # 3. Directory skills (~/.agent-sandbox/{common, <engine>}/skills/<name>)
        if engine.skills and engine.skills.target_dir:
            dir_skills = dir_skill_resolver.resolve_skills_for_engine(engine.name)
            container_skills_base = _to_container_path(engine.skills.target_dir)
            for s_name, s_host_path in sorted(dir_skills.items()):
                s_container_path = os.path.join(container_skills_base, s_name)
                volumes.append(f"{s_host_path}:{s_container_path}:ro")

        # 4. Catalog skills (skills.json discovered paths outside workspaces)
        catalog_skills = catalog_skill_resolver.resolve_for_engine(engine, warn=False)
        for c_path in sorted(catalog_skills):
            volumes.append(f"{c_path}:{c_path}:ro")

        # 5. Whitelisted workspaces (mounted to identical host paths with :cached)
        for ws in config.allowed_workspaces:
            if not ws:
                continue
            ws_norm = os.path.abspath(os.path.expanduser(ws)).rstrip("/")
            if os.path.isdir(ws_norm):
                volumes.append(f"{ws_norm}:{ws_norm}:cached")

        service_entry["volumes"] = volumes
        services[engine.name] = service_entry

    for engine in active_engines:
        vname = f"agent_home_{engine.name}"
        volumes_top[vname] = {"name": vname}

    override_data = {"services": services}
    if volumes_top:
        override_data["volumes"] = volumes_top

    yaml_content = yaml.safe_dump(override_data, sort_keys=False)

    if out_path:
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(yaml_content)

    return yaml_content
