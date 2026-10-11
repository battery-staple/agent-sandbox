"""Narrow static guardrail: catalog-derived host paths must not reach a Docker
bind-mount source without a containment check.

Source (untrusted): ``entries[].path`` strings from agent-writable JSON skill
catalogs, parsed in ``skills/catalog.py`` (``parse_catalogs`` -> ``json.load``).

Sink (privileged): Docker bind-mount source emitted in
``compose/generator.py`` (``volumes.append(f"{c_path}:{c_path}:ro")``).

Guard (required between source and sink): symlink-resolved containment of the
resolved path inside a host-only allowlist root (``realpath`` + prefix check,
wired into the resolution entry points so the validated canonical path is
exactly what gets mounted).

Exit 0 = guard present and wired (clean). Exit 1 = sink reachable without the
guard (flagged). Stdlib only.
"""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = (
    REPO_ROOT / "packages/agent-sandbox/src/agent_sandbox/skills/catalog.py"
)
DEFAULT_GENERATOR = (
    REPO_ROOT / "packages/agent-sandbox/src/agent_sandbox/compose/generator.py"
)

ENTRY_POINTS = ("resolve_for_engine", "resolve_with_rejections")


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as e:
        print(f"check_untrusted_mounts: cannot parse {path}: {e}", file=sys.stderr)
        return None


def _class_by_name(tree: ast.Module, name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    return None


def _uses_realpath(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if isinstance(node, ast.Attribute) and node.attr == "realpath":
            return True
    return False


def _uses_startswith(fn: ast.FunctionDef) -> bool:
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
        ):
            return True
    return False


def _called_names(fn: ast.FunctionDef) -> set[str]:
    """Bare ``foo()`` and ``self.foo()`` callee names referenced in ``fn``."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "self"
        ):
            names.add(func.attr)
    return names


def catalog_guard_present(catalog_path: Path) -> tuple[bool, str]:
    """Returns (ok, reason) for the containment guard in catalog.py.

    Reachability analysis over ``CatalogSkillResolver`` methods plus
    module-level helpers in the same file:
      * containment guard = uses ``realpath`` AND (uses ``startswith``
        itself OR transitively reaches a helper that does);
      * reachable = transitive ``self.<method>()`` / bare-call closure
        from ``resolve_for_engine`` (and ``resolve_with_rejections``
        when present).
    Guard OK iff a containment guard is reachable from an entry point.
    """
    tree = _parse(catalog_path)
    if tree is None:
        return False, "unparseable catalog.py (fail-closed)"
    cls = _class_by_name(tree, "CatalogSkillResolver")
    if cls is None:
        return False, "CatalogSkillResolver class not found (fail-closed)"

    funcs: dict[str, ast.FunctionDef] = {}
    for node in cls.body:
        if isinstance(node, ast.FunctionDef):
            funcs[node.name] = node
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            funcs.setdefault(node.name, node)

    entries = [e for e in ENTRY_POINTS if e in funcs]
    if not entries:
        return False, "resolve_for_engine not found (fail-closed)"

    uses_realpath = {n: _uses_realpath(f) for n, f in funcs.items()}
    uses_starts = {n: _uses_startswith(f) for n, f in funcs.items()}
    calls = {n: _called_names(f) & funcs.keys() for n, f in funcs.items()}

    # Transitive closure of "reaches a startswith user" (fixpoint).
    reaches_starts: dict[str, bool] = dict(uses_starts)
    changed = True
    while changed:
        changed = False
        for name, callees in calls.items():
            if not reaches_starts[name] and any(
                reaches_starts.get(c, False) for c in callees
            ):
                reaches_starts[name] = True
                changed = True

    guards = {
        n for n in funcs if uses_realpath[n] and reaches_starts.get(n, False)
    }

    # Transitive reachability from the entry points.
    reachable: set[str] = set()
    stack = list(entries)
    while stack:
        name = stack.pop()
        if name in reachable or name not in funcs:
            continue
        reachable.add(name)
        stack.extend(calls[name] - reachable)

    wired = sorted(guards & reachable)
    if wired:
        return True, (
            "containment guard reachable from entry point: "
            + ", ".join(wired)
            + " (entries: "
            + ", ".join(sorted(set(entries) & reachable))
            + ")"
        )
    if not guards:
        return False, "missing guard: no realpath+prefix containment helper found"
    return False, (
        "missing guard: containment helper(s) "
        + ", ".join(sorted(guards))
        + " not reachable from "
        + "/".join(entries)
    )


def generator_sink_present(generator_path: Path) -> tuple[bool, str]:
    """Returns (found, reason) for the catalog->bind-mount sink in generator.py."""
    tree = _parse(generator_path)
    if tree is None:
        return True, "unparseable generator.py (fail-closed: assume sink present)"
    src = generator_path.read_text(encoding="utf-8")
    # Sink pattern: loop over catalog_skills / resolve_for_engine result and
    # append a "<host>:<container>[:mode]" volume string.
    has_catalog_call = (
        "resolve_for_engine" in src or "catalog_skill" in src.lower()
    )
    has_mount_append = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "append":
            for arg in node.args:
                frag = ast.dump(arg)
                if "c_path" in frag or "catalog" in frag:
                    has_mount_append = True
                    break
        if has_mount_append:
            break
    # Fallback textual check for the exact vulnerable idiom.
    if not has_mount_append and ("c_path}:{c_path}" in src or "{c_path}" in src):
        has_mount_append = True
    if has_catalog_call and has_mount_append:
        return True, "catalog-derived value formatted as bind-mount source (volumes.append ... c_path)"
    return False, "no catalog->bind-mount sink pattern found"


def check(catalog_path: Path, generator_path: Path) -> int:
    sink_found, sink_reason = generator_sink_present(generator_path)
    guard_ok, guard_reason = catalog_guard_present(catalog_path)
    print(f"catalog:   {catalog_path}")
    print(f"  guard: {'OK' if guard_ok else 'MISSING'} ({guard_reason})")
    print(f"generator: {generator_path}")
    print(f"  sink:  {'present' if sink_found else 'absent'} ({sink_reason})")
    if sink_found and not guard_ok:
        print(
            "FAIL: host path derived from JSON catalog content reaches a Docker "
            "bind-mount source without a containment/allowlist guard"
        )
        print("  source: skills/catalog.py parse_catalogs entries[].path (json.load)")
        print("  sink:   compose/generator.py volumes.append(f\"{c_path}:{c_path}:ro\")")
        print("  fix:    gate resolution entry points through a realpath+prefix containment helper")
        return 1
    print("PASS: catalog-derived mount source is containment-guarded")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    ap.add_argument("--generator", type=Path, default=DEFAULT_GENERATOR)
    args = ap.parse_args(argv)
    return check(args.catalog, args.generator)


if __name__ == "__main__":
    raise SystemExit(main())
