"""Engine discovery and registry for pluggable agent engines."""
from __future__ import annotations

import os
from typing import Sequence
from ..config.loader import load_engine_manifest
from ..config.models import EngineManifest


class EngineRegistry:
    """Registry maintaining available engine manifests keyed by parsed manifest.name."""

    def __init__(self) -> None:
        self._engines: dict[str, EngineManifest] = {}

    def register(self, manifest: EngineManifest) -> None:
        if manifest.name in self._engines:
            raise ValueError(f"Duplicate engine registered with name '{manifest.name}'")
        self._engines[manifest.name] = manifest

    def get(self, name: str) -> EngineManifest:
        if name not in self._engines:
            available = ", ".join(sorted(self._engines.keys())) or "none"
            raise KeyError(f"Unknown engine '{name}'. Available engines: {available}")
        return self._engines[name]

    def has(self, name: str) -> bool:
        return name in self._engines

    def list_all(self) -> list[EngineManifest]:
        return [self._engines[k] for k in sorted(self._engines.keys())]

    def resolve_active(
        self,
        requested_names: Sequence[str] | None = None,
    ) -> list[EngineManifest]:
        """Resolves active engines from requested engine names."""
        if not requested_names:
            return []
        resolved: list[EngineManifest] = []
        seen = set()

        for name in requested_names:
            if not name or name in seen:
                continue
            engine = self.get(name)
            resolved.append(engine)
            seen.add(name)

        return resolved


def discover_engines(engines_dir: str) -> EngineRegistry:
    """Scans engines directory for subdirectories containing manifest.yaml."""
    registry = EngineRegistry()
    norm_dir = os.path.abspath(engines_dir)

    if not os.path.isdir(norm_dir):
        return registry

    for entry in sorted(os.listdir(norm_dir)):
        subpath = os.path.join(norm_dir, entry)
        if not os.path.isdir(subpath):
            continue

        manifest_candidate = os.path.join(subpath, "manifest.yaml")
        if not os.path.isfile(manifest_candidate):
            manifest_candidate = os.path.join(subpath, "manifest.yml")
            if not os.path.isfile(manifest_candidate):
                continue

        manifest = load_engine_manifest(manifest_candidate, expected_dir_name=entry)
        registry.register(manifest)

    return registry
