"""Volume management and legacy volume migration for agent sandbox."""
from __future__ import annotations

import subprocess
import sys


def check_volume_exists(volume_name: str) -> bool:
    """Checks if a Docker volume exists."""
    try:
        res = subprocess.run(
            ["docker", "volume", "ls", "--filter", f"name=^{volume_name}$", "--format", "{{.Name}}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0:
            lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
            return volume_name in lines
    except Exception:
        pass
    return False


def migrate_antigravity_volume_if_needed() -> bool:
    """
    Migrates legacy volume 'antigravity_home_persist' to 'agent_home_antigravity'
    if the legacy volume exists and the new volume does not.
    """
    legacy_vol = "antigravity_home_persist"
    new_vol = "agent_home_antigravity"

    if not check_volume_exists(legacy_vol):
        return False

    if check_volume_exists(new_vol):
        return False

    print(f"[Sandbox Migration] Detected legacy volume '{legacy_vol}'.", file=sys.stderr)
    print(f"[Sandbox Migration] Migrating data from '{legacy_vol}' to '{new_vol}'...", file=sys.stderr)

    try:
        # Create new volume
        create_res = subprocess.run(
            ["docker", "volume", "create", new_vol],
            capture_output=True,
            text=True,
            check=False,
        )
        if create_res.returncode != 0:
            print(f"[Sandbox Migration Error] Failed to create volume '{new_vol}': {create_res.stderr}", file=sys.stderr)
            return False

        # Copy data via a transient container
        copy_res = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-v",
                f"{legacy_vol}:/from:ro",
                "-v",
                f"{new_vol}:/to",
                "ubuntu:24.04",
                "bash",
                "-c",
                "cp -a /from/. /to/",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if copy_res.returncode != 0:
            print(f"[Sandbox Migration Error] Data transfer failed: {copy_res.stderr}", file=sys.stderr)
            return False

        print(f"[Sandbox Migration] Successfully migrated '{legacy_vol}' -> '{new_vol}'.", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[Sandbox Migration Error] Exception during volume migration: {e}", file=sys.stderr)
        return False
