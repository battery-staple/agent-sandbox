#!/usr/bin/env python3
"""Validate every engine manifest against config/engine-manifest.schema.json.

Uses the same loader the CLI uses at runtime (load_engine_manifest), so a
manifest that passes here is guaranteed to load when an engine is started.
Exits non-zero and prints every failure, which makes it suitable for CI.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PKG_SRC = REPO_ROOT / "packages" / "agent-sandbox" / "src"

sys.path.insert(0, str(PKG_SRC))

from agent_sandbox.config.loader import load_engine_manifest  # noqa: E402


def main() -> int:
    schema_path = REPO_ROOT / "config" / "engine-manifest.schema.json"
    manifests = sorted((REPO_ROOT / "engines").glob("*/manifest.yaml"))

    if not manifests:
        print("ERROR: no engine manifests found under engines/")
        return 1

    failures = []
    for manifest in manifests:
        rel = manifest.relative_to(REPO_ROOT)
        try:
            loaded = load_engine_manifest(str(manifest), schema_path=str(schema_path))
        except Exception as exc:
            failures.append(f"  {rel}: {exc}")
            continue
        print(f"OK: {rel} (engine '{loaded.name}', port {loaded.port})")

    if failures:
        print("Manifest validation failed:")
        for failure in failures:
            print(failure)
        return 1

    print(f"All {len(manifests)} engine manifest(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
