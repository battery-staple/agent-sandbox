"""Catalog-based skill resolution (e.g. skills.json) with recursive inheritance and conflict pruning."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from typing import Mapping, Sequence
from ..config.models import EngineManifest
from .directory import skill_dirs


def _is_within(path: str, ancestor: str) -> bool:
    """True when path equals ancestor or lies under it."""
    if path == ancestor:
        return True
    return path.startswith(ancestor.rstrip(os.sep) + os.sep)


@dataclass(frozen=True)
class CatalogRejection:
    path: str
    source_catalog: str
    reason: str


class CatalogSkillResolver:
    """Discovers and resolves skill directories declared in JSON skill catalogs."""

    # Bound on inherits-chain depth and total files parsed per resolve.
    MAX_INHERIT_DEPTH = 32
    MAX_CATALOG_FILES = 256

    def __init__(
        self,
        allowed_workspaces: Sequence[str] | None = None,
        allowed_skill_roots: Sequence[str] | None = None,
        sandbox_dir: str | None = None,
    ) -> None:
        self.allowed_workspaces = [
            os.path.abspath(os.path.expanduser(w))
            for w in (allowed_workspaces or ())
            if w and os.path.isdir(os.path.abspath(os.path.expanduser(w)))
        ]
        # Host-only, agent-inaccessible roots. The sandbox dir itself is not
        # trusted wholesale (it also holds logs, IPC secrets, and config); only
        # its skills subtrees are added, per engine, in resolve_for_engine.
        self.sandbox_dir = os.path.abspath(os.path.expanduser(sandbox_dir)) if sandbox_dir else None
        self.allowed_skill_roots = [
            os.path.abspath(os.path.expanduser(r))
            for r in (allowed_skill_roots or ())
            if r and os.path.isdir(os.path.abspath(os.path.expanduser(r)))
        ]
        self.last_rejections: list[CatalogRejection] = []
        self._warn_agent_writable_roots()

    def _warn_agent_writable_roots(self) -> None:
        # A skill root inside an agent-writable workspace is not a real
        # boundary: the agent could swap symlinks there after validation.
        for root in self.allowed_skill_roots:
            for ws in self.allowed_workspaces:
                if _is_within(os.path.realpath(root), os.path.realpath(ws)):
                    print(
                        f"[Sandbox Warning] allowed_skill_roots entry {root} is inside "
                        f"agent-writable workspace {ws}; containment there is not a real boundary.",
                        file=sys.stderr,
                    )
                    break

    def _roots_for_engine(self, engine: EngineManifest) -> list[str]:
        """Explicit allowed roots plus the engine's host-only skill directories."""
        roots = list(self.allowed_skill_roots)
        if self.sandbox_dir:
            for candidate in skill_dirs(self.sandbox_dir, engine.name):
                if os.path.isdir(candidate):
                    roots.append(candidate)
        return roots

    @staticmethod
    def _allowed_realpath(path: str, canonical_roots: Sequence[str]) -> str | None:
        """Returns path's canonical location if it is inside a host-only root.

        Only roots the sandboxed agent cannot write to are considered: whitelisted
        workspaces are deliberately excluded because they are agent-writable, so a
        symlink swap could redirect a mount after validation. ``canonical_roots``
        must already be realpath-resolved. The single ``realpath`` resolution of
        ``path`` is returned directly (never recomputed) so the validated path is
        exactly the path that gets mounted. Returns None when it escapes every root.
        """
        real_path = os.path.realpath(path)
        for root in canonical_roots:
            if _is_within(real_path, root):
                return real_path
        return None

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
        warned: set[str] = set()

        def _warn_once(key: str, message: str) -> None:
            if key not in warned:
                warned.add(key)
                print(message, file=sys.stderr)

        def _parse_file(file_path: str, depth: int = 0) -> None:
            if depth > self.MAX_INHERIT_DEPTH:
                _warn_once(
                    "depth",
                    f"[Sandbox Warning] Skill catalog inherits depth exceeds "
                    f"{self.MAX_INHERIT_DEPTH}, stopping at {file_path}",
                )
                return
            if len(visited_files) >= self.MAX_CATALOG_FILES:
                _warn_once(
                    "count",
                    f"[Sandbox Warning] Too many skill catalog files "
                    f"(>{self.MAX_CATALOG_FILES}), stopping traversal.",
                )
                return
            try:
                canonical = os.path.realpath(file_path)
            except OSError:
                return
            if canonical in visited_files:
                return
            if not os.path.isfile(canonical):
                return
            visited_files.add(canonical)
            norm = canonical

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
                        _parse_file(resolved_inherit, depth + 1)

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

    @staticmethod
    def _resolve_entry_path(item: dict[str, str]) -> str:
        """Resolves a raw catalog entry to an absolute host path."""
        raw_path = item["path"]
        base_dir = item["base_dir"]
        if raw_path.startswith("~"):
            resolved = os.path.abspath(os.path.expanduser(raw_path))
        elif os.path.isabs(raw_path):
            resolved = os.path.abspath(raw_path)
        else:
            resolved = os.path.abspath(os.path.join(base_dir, raw_path))
        return resolved.rstrip("/")

    def resolve_and_validate_paths(self, raw_entries: Sequence[dict[str, str]], warn: bool = True) -> list[str]:
        """Resolves path entries and verifies directory existence on host."""
        valid: list[str] = []
        seen: set[str] = set()

        for item in raw_entries:
            resolved = self._resolve_entry_path(item)
            source = item.get("source_config", "unknown")

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
        # 1. Filter out skills inside an active workspace
        uncovered: list[str] = []
        for s in skill_paths:
            norm_s = os.path.abspath(s)
            if not any(_is_within(norm_s, os.path.abspath(w)) for w in self.allowed_workspaces):
                uncovered.append(norm_s)

        # 2. Deduplicate nested skill paths (keep ancestor)
        uncovered.sort(key=lambda p: (len(p.split("/")), p))
        final_skills: list[str] = []
        for s in uncovered:
            if any(_is_within(s, parent) and s != parent for parent in final_skills):
                continue
            final_skills.append(s)

        return final_skills

    def filter_allowed_roots(
        self,
        skill_paths: Sequence[str],
        roots: Sequence[str],
        warn: bool = True,
        sources: Mapping[str, str] | None = None,
    ) -> list[str]:
        """Restricts catalog skills to host-only roots and returns canonical paths.

        Catalog files can live in agent-writable locations, so a resolved path is
        only mounted when its real (symlink-resolved) location falls inside a root
        the agent cannot write to. The canonical path from that single resolution
        is what gets mounted, so a symlink swapped between validation and
        ``docker compose up`` cannot redirect the bind mount. Rejections are
        recorded on ``last_rejections`` and only printed when ``warn`` is true,
        so silent callers (e.g. status displays) can report them their own way.
        """
        allowed: list[str] = []
        seen: set[str] = set()
        real_roots = [os.path.realpath(r) for r in roots]
        rejections: list[CatalogRejection] = []
        for path in skill_paths:
            canonical = self._allowed_realpath(path, real_roots)
            if canonical is None:
                source = sources.get(path, "unknown") if sources else "unknown"
                rejections.append(
                    CatalogRejection(
                        path=path,
                        source_catalog=source,
                        reason="outside allowed skill roots",
                    )
                )
                if warn:
                    print(
                        f"[Sandbox Warning] Refusing to mount catalog skill outside allowed roots: "
                        f"{path} (referenced in {source}). "
                        f"Run 'agent-sandbox skill-root add <dir>' to allow it.",
                        file=sys.stderr,
                    )
                continue
            if canonical not in seen:
                seen.add(canonical)
                allowed.append(canonical)
        self.last_rejections = rejections
        return sorted(allowed)

    def resolve_with_rejections(
        self, engine: EngineManifest, warn: bool = True
    ) -> tuple[list[str], list[CatalogRejection]]:
        """Resolves catalog skills and returns accepted paths plus rejections."""
        catalog_files = self.find_catalog_files(engine)
        raw_entries = self.parse_catalogs(catalog_files)
        sources: dict[str, str] = {}
        for item in raw_entries:
            sources.setdefault(
                self._resolve_entry_path(item), item.get("source_config", "unknown")
            )
        valid_paths = self.resolve_and_validate_paths(raw_entries, warn=warn)
        uncovered = self.filter_conflicts(valid_paths)
        accepted = self.filter_allowed_roots(
            uncovered, self._roots_for_engine(engine), warn=warn, sources=sources
        )
        return accepted, list(self.last_rejections)

    def resolve_for_engine(self, engine: EngineManifest, warn: bool = True) -> list[str]:
        """Runs the complete catalog discovery and resolution pipeline for an engine."""
        accepted, _ = self.resolve_with_rejections(engine, warn=warn)
        return accepted
