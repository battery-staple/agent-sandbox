"""Central YAML loader and schema validation for engine manifests and sandbox configuration."""
from __future__ import annotations

import json
import os
from typing import Any, Mapping
import yaml
import jsonschema

from .models import EngineManifest, EngineMountConfig, EngineRuleConfig, EngineSkillConfig, SandboxConfig


def validate_manifest_dict(data: Any, schema_path: str | None = None) -> None:
    """Validates manifest dictionary against the formal JSON schema. Fail fast."""
    if not isinstance(data, dict):
        raise ValueError("Engine manifest must be a YAML mapping/object.")

    if schema_path is None:
        schema_path = os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "..", "..", "..", "config", "engine-manifest.schema.json"
            )
        )
    if not os.path.isfile(schema_path):
        raise FileNotFoundError(f"Engine manifest schema not found: {schema_path}")

    with open(schema_path, "r", encoding="utf-8") as sf:
        schema_json = json.load(sf)
    try:
        jsonschema.validate(instance=data, schema=schema_json)
    except Exception as e:
        raise ValueError(f"Schema validation failed: {e}") from e


def load_engine_manifest(manifest_path: str, expected_dir_name: str | None = None, schema_path: str | None = None) -> EngineManifest:
    """Loads, validates, and parses an engine manifest file."""
    norm_path = os.path.abspath(manifest_path)
    if not os.path.isfile(norm_path):
        raise FileNotFoundError(f"Engine manifest not found: {norm_path}")

    with open(norm_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    validate_manifest_dict(data, schema_path=schema_path)

    parent_dir = os.path.basename(os.path.dirname(norm_path))
    check_dir = expected_dir_name or parent_dir
    if data["name"] != check_dir:
        raise ValueError(
            f"Manifest name '{data['name']}' does not match its parent directory '{check_dir}'."
        )

    # Construct immutable EngineRuleConfig
    rule_cfg = None
    if "rules" in data:
        r_data = data["rules"]
        rule_cfg = EngineRuleConfig(
            target_file=r_data["target_file"],
            host_sources=tuple(r_data.get("host_sources", ())),
        )

    # Construct immutable EngineSkillConfig
    skill_cfg = None
    if "skills" in data:
        s_data = data["skills"]
        skill_cfg = EngineSkillConfig(
            target_dir=s_data["target_dir"],
            catalogs=tuple(s_data.get("catalogs", ())),
        )

    mounts = tuple(data.get("mounts", ()))

    return EngineManifest(
        name=data["name"],
        port=int(data["port"]),
        web_url=data["web_url"],
        rules=rule_cfg,
        skills=skill_cfg,
        mounts=mounts,
        directory=os.path.dirname(norm_path),
    )


def load_sandbox_config(config_path: str) -> SandboxConfig:
    """Loads and validates SandboxConfig from YAML file."""
    norm_path = os.path.abspath(config_path)
    if not os.path.isfile(norm_path):
        return SandboxConfig()

    with open(norm_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        return SandboxConfig()

    allowed_workspaces = tuple(data.get("allowed_workspaces") or ())
    allowed_commands = data.get("allowed_commands") or {}

    return SandboxConfig(
        allowed_workspaces=allowed_workspaces,
        allowed_commands=allowed_commands,
    )


def save_sandbox_config(config: SandboxConfig, config_path: str) -> None:
    """Saves SandboxConfig back to YAML file, preserving formatting."""
    norm_path = os.path.abspath(config_path)
    os.makedirs(os.path.dirname(norm_path), exist_ok=True)

    data = {
        "allowed_workspaces": list(config.allowed_workspaces),
        "allowed_commands": config.allowed_commands,
    }

    with open(norm_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
