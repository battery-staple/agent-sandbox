"""Central YAML loader and schema validation for engine manifests and sandbox configuration."""
from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping
import yaml

from .models import EngineManifest, EngineMountConfig, EngineRuleConfig, EngineSkillConfig, SandboxConfig

ENGINE_NAME_REGEX = re.compile(r"^[a-z0-9_-]+$")


def validate_manifest_dict(data: Any, schema_path: str | None = None) -> None:
    """Validates manifest dictionary against schema rules."""
    if not isinstance(data, dict):
        raise ValueError("Engine manifest must be a YAML mapping/object.")

    # Try formal jsonschema validation if available and schema file exists
    if schema_path is None:
        # Default candidate location relative to repository root
        candidate = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "config", "engine-manifest.schema.json")
        )
        if os.path.isfile(candidate):
            schema_path = candidate

    if schema_path and os.path.isfile(schema_path):
        try:
            import jsonschema  # type: ignore
            with open(schema_path, "r", encoding="utf-8") as sf:
                schema_json = json.load(sf)
            jsonschema.validate(instance=data, schema=schema_json)
            return
        except ImportError:
            pass  # Fallback to internal strict validation
        except Exception as e:
            raise ValueError(f"Schema validation failed: {e}") from e

    # Internal strict validation matching Draft 2020-12 schema
    allowed_top_keys = {"name", "port", "web_url", "rules", "skills", "mounts"}
    extra_keys = set(data.keys()) - allowed_top_keys
    if extra_keys:
        raise ValueError(f"Manifest contains unexpected properties: {sorted(extra_keys)}")

    # Required fields
    for req in ("name", "port", "web_url"):
        if req not in data:
            raise ValueError(f"Manifest missing required property: '{req}'")

    name = data["name"]
    if not isinstance(name, str) or not ENGINE_NAME_REGEX.match(name):
        raise ValueError(f"Invalid engine name '{name}': must match '^[a-z0-9_-]+$'")

    port = data["port"]
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        raise ValueError(f"Invalid engine port '{port}': must be an integer between 1 and 65535")

    web_url = data["web_url"]
    if not isinstance(web_url, str) or not web_url.strip():
        raise ValueError("Engine web_url must be a non-empty string.")

    # Rules validation
    if "rules" in data:
        rules = data["rules"]
        if not isinstance(rules, dict):
            raise ValueError("Property 'rules' must be an object.")
        extra_rules_keys = set(rules.keys()) - {"target_file", "host_sources"}
        if extra_rules_keys:
            raise ValueError(f"Property 'rules' contains unexpected keys: {sorted(extra_rules_keys)}")
        if "target_file" not in rules or not isinstance(rules["target_file"], str) or not rules["target_file"].strip():
            raise ValueError("Property 'rules.target_file' is required and must be a string.")
        if "host_sources" in rules:
            if not isinstance(rules["host_sources"], list) or not all(isinstance(s, str) for s in rules["host_sources"]):
                raise ValueError("Property 'rules.host_sources' must be a list of strings.")

    # Skills validation
    if "skills" in data:
        skills = data["skills"]
        if not isinstance(skills, dict):
            raise ValueError("Property 'skills' must be an object.")
        extra_skills_keys = set(skills.keys()) - {"target_dir", "catalogs"}
        if extra_skills_keys:
            raise ValueError(f"Property 'skills' contains unexpected keys: {sorted(extra_skills_keys)}")
        if "target_dir" not in skills or not isinstance(skills["target_dir"], str) or not skills["target_dir"].strip():
            raise ValueError("Property 'skills.target_dir' is required and must be a string.")
        if "catalogs" in skills:
            if not isinstance(skills["catalogs"], list) or not all(isinstance(s, str) for s in skills["catalogs"]):
                raise ValueError("Property 'skills.catalogs' must be a list of strings.")

    # Mounts validation
    if "mounts" in data:
        mounts = data["mounts"]
        if not isinstance(mounts, list) or not all(isinstance(m, str) for m in mounts):
            raise ValueError("Property 'mounts' must be a list of strings.")


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
