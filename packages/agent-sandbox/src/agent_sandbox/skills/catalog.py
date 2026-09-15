"""Catalog-based skill resolution (e.g. skills.json) with recursive inheritance and conflict pruning."""
from __future__ import annotations

import json
import os
import sys
from typing import Sequence
from ..config.models import EngineManifest


class CatalogSkillResolver:
    """Discovers and resolves skill directories declared in JSON skill catalogs."""

    def __init__(self, allowed_workspaces: Sequence[str] | None = None) -> None:
        self.allowed_workspaces = [
            os.path.abspath(os.path.expanduser(w))
            for w in (allowed_workspaces or ())
            if w and os.path.isdir(os.path.abspath(os.path.expanduser(w)))
        ]

    def find_catalog_files(self, engine: EngineManifest) -> list[str]:
        """Locates catalog files declared by the engine and present in workspaces."""
        if not engine.skills or not engine.skills.catalogs:
            return []

        catalog_patterns = engine.skills.catalogs
        found_files: list[str] = []

        for pattern in catalog_patterns:
            if pattern.startswith("~") or os.path.isabs(pattern):
                expanded = os.path.abspath(os.path.expanduser(pattern))
                if os.path.isfile(expanded) and expanded not in found_files:
                    found_files.append(expanded)
            else:
                # Relative pattern: scan each active workspace
                for ws in self.allowed_workspaces:
                    candidate = os.path.abspath(os.path.join(ws, pattern))
                    if os.path.isfile(candidate) and candidate not in found_files:
                        found_files.append(candidate)

        # Standard workspace candidate fallbacks if catalogs are enabled for this engine
        workspace_candidates = [
            os.path.join(".agents", "skills.json"),
            os.path.join(".agent", "skills.json"),
            os.path.join("_agents", "skills.json"),
            os.path.join("_agent", "skills.json"),
            "skills.json",
        ]
        for ws in self.allowed_workspaces:
            for cand in workspace_candidates:
                cand_path = os.path.abspath(os.path.join(ws, cand))
                if os.path.isfile(cand_path) and cand_path not in found_files:
                    found_files.append(cand_path)

        return found_files

    def parse_catalogs(self, catalog_files: Sequence[str]) -> list[dict[str, str]]:
        """Parses JSON catalog files traversing 'inherits' chains with cycle detection."""
        raw_entries: list[dict[str, str]] = []
        visited_files: set[str] = set()

        def _parse_file(file_path: str) -> None:
            norm = os.path.abspath(file_path)
            if norm in visited_files or not os.path.isfile(norm):
                return
            visited_files.add(norm)

            try:
                with open(norm, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                print(f"[Sandbox Warning] Failed to parse skill catalog {norm}: {e}", file=sys.stderr)
                return

            if not isinstance(data, dict):
                return

            base_dir = os.path.dirname(norm)

            # Process 'inherits'
            inherits = data.get("inherits", [])
            if isinstance(inherits, list):
                for item in inherits:
                    inherit_path = item.get("path") if isinstance(item, dict) else item
                    if isinstance(inherit_path, str) and inherit_path.strip():
                        inherit_path = inherit_path.strip()
                        if inherit_path.startswith("~"):
                            resolved_inherit = os.path.expanduser(inherit_path)
                        elif os.path.isabs(inherit_path):
                            resolved_inherit = inherit_path
                        else:
                            resolved_inherit = os.path.join(base_dir, inherit_path)
                        _parse_file(resolved_inherit)

            # Process 'entries'
            entries = data.get("entries", [])
            if isinstance(entries, list):
                for entry in entries:
                    entry_path = entry.get("path") if isinstance(entry, dict) else entry
                    if isinstance(entry_path, str) and entry_path.strip():
                        raw_entries.append({
                            "path": entry_path.strip(),
                            "base_dir": base_dir,
                            "source_config": norm,
                        })

        for cfg in catalog_files:
            _parse_file(cfg)

        return raw_entries

    def resolve_and_validate_paths(self, raw_entries: Sequence[dict[str, str]], warn: bool = True) -> list[str]:
        """Resolves path entries and verifies directory existence on host."""
        valid: list[str] = []
        seen: set[str] = set()

        for item in raw_entries:
            raw_path = item["path"]
            base_dir = item["base_dir"]
            source = item.get("source_config", "unknown")

            if raw_path.startswith("~"):
                resolved = os.path.abspath(os.path.expanduser(raw_path))
            elif os.path.isabs(raw_path):
                resolved = os.path.abspath(raw_path)
            else:
                resolved = os.path.abspath(os.path.join(base_dir, raw_path))

            resolved = resolved.rstrip("/")
            if resolved in seen:
                continue
            seen.add(resolved)

            if os.path.isdir(resolved):
                valid.append(resolved)
            elif warn:
                print(
                    f"[Sandbox Warning] Skill directory not found on host: {resolved} (referenced in {source})",
                    file=sys.stderr,
                )

        return valid

    def filter_conflicts(self, skill_paths: Sequence[str]) -> list[str]:
        """Prunes skills covered by workspaces and deduplicates nested skills."""
        normalized_ws = [w.rstrip("/") for w in self.allowed_workspaces]

        # 1. Filter out skills inside an active workspace
        uncovered: list[str] = []
        for s in skill_paths:
            norm_s = os.path.abspath(s).rstrip("/")
            if not any(norm_s == w or norm_s.startswith(w + "/") for w in normalized_ws):
                uncovered.append(norm_s)

        # 2. Deduplicate nested skill paths (keep ancestor)
        uncovered.sort(key=lambda p: (len(p.split("/")), p))
        final_skills: list[str] = []
        for s in uncovered:
            if any(s.startswith(parent + "/") for parent in final_skills):
                continue
            final_skills.append(s)

        return final_skills

    def resolve_for_engine(self, engine: EngineManifest, warn: bool = True) -> list[str]:
        """Runs the complete catalog discovery and resolution pipeline for an engine."""
        catalog_files = self.find_catalog_files(engine)
        raw_entries = self.parse_catalogs(catalog_files)
        valid_paths = self.resolve_and_validate_paths(raw_entries, warn=warn)
        return self.filter_conflicts(valid_paths)
