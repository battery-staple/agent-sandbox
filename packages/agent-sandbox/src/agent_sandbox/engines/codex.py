"""Codex engine adapter. All Codex-specific user-config knowledge lives here."""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .base import EngineAdapter

CONFIG_FILENAME = "config.yaml"

#: Host desktop apps that bundle a `codex` binary, first existing wins.
HOST_APP_CANDIDATES = ("/Applications/ChatGPT.app", "/Applications/Codex.app")

_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


@dataclass(frozen=True)
class CodexUserConfig:
    """No supported user-config keys yet (device-auth only, no API key)."""


def parse_codex_user_config(path: Path) -> CodexUserConfig:
    """Parses ~/.agent-sandbox/codex/config.yaml; no supported user keys yet."""
    if not path.is_file():
        return CodexUserConfig()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return CodexUserConfig()
    if not isinstance(data, dict):
        raise ValueError(f"Invalid codex config {path}: top level must be a mapping.")
    extra = set(data.keys())
    if extra:
        raise ValueError(f"Invalid codex config {path}: unexpected keys: {sorted(extra)}")
    return CodexUserConfig()


def _first_version(text: str) -> str | None:
    """Returns the first x.y.z-looking token from `text`, or None."""
    match = _VERSION_RE.search(text or "")
    return match.group(0) if match else None


def _container_codex_version() -> str | None:
    """Container `codex --version` via docker exec; None when unavailable."""
    from . import CONTAINER_NAME_PREFIX  # deferred: avoids a circular import at module load

    result = subprocess.run(
        ["docker", "exec", f"{CONTAINER_NAME_PREFIX}codex", "codex", "--version"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return _first_version(result.stdout)


def _host_app_codex_version() -> str | None:
    """Version from the desktop app's bundled codex binary; first existing app wins."""
    for app in HOST_APP_CANDIDATES:
        binary = Path(app) / "Contents" / "Resources" / "codex"
        if not binary.is_file():
            continue
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
        return _first_version(result.stdout)
    return None


class CodexAdapter(EngineAdapter):
    name = "codex"

    def default_config(self) -> CodexUserConfig:
        return CodexUserConfig()

    def parse_user_config(self, path: Path) -> CodexUserConfig:
        return parse_codex_user_config(path)

    def config_path(self, sandbox_dir: Path) -> Path:
        return sandbox_dir / self.name / CONFIG_FILENAME

    def scaffold(self, sandbox_dir: Path, repo_root: Path) -> None:
        # No host-side config file; the container entrypoint seeds
        # ~/.codex/config.toml from config.toml.example on first start.
        return None

    def compose_env(self, config: object) -> tuple[str, ...]:
        if not isinstance(config, CodexUserConfig):
            raise TypeError(f"CodexAdapter.compose_env expects CodexUserConfig, got {type(config).__name__}")
        return ()

    def status_lines(self, config: object) -> tuple[str, ...]:
        if not isinstance(config, CodexUserConfig):
            raise TypeError(f"CodexAdapter.status_lines expects CodexUserConfig, got {type(config).__name__}")
        return (
            "Connection: SSH remote (alias agent-sandbox-codex).",
            "Auth: use the Codex desktop app / agent-sandbox shell codex + codex login --device-auth.",
        )

    def preflight_warnings(self, sandbox_dir: Path) -> tuple[str, ...]:
        """Warns when container and desktop-app codex versions differ. Never raises."""
        try:
            container_version = _container_codex_version()
            if not container_version:
                return ()
            host_version = _host_app_codex_version()
            if not host_version or host_version == container_version:
                return ()
            return (
                f"Codex version mismatch: container codex {container_version} vs "
                f"desktop app codex {host_version}. The desktop app may have updated; "
                "rebuild the agent-sandbox image to refresh the container Codex.",
            )
        except Exception:
            return ()
