"""OpenCode engine adapter. All OpenCode-specific user-config knowledge lives here."""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .base import EngineAdapter


@dataclass(frozen=True)
class OpencodeServerConfig:
    username: str = "opencode"
    password: str | None = None


@dataclass(frozen=True)
class OpencodeUserConfig:
    server: OpencodeServerConfig = field(default_factory=OpencodeServerConfig)


EXAMPLE_REL = Path("engines") / "opencode" / "config.example.yaml"
CONFIG_FILENAME = "config.yaml"


def _parse_server(data: object, path: Path) -> OpencodeServerConfig:
    if data is None:
        return OpencodeServerConfig()
    if not isinstance(data, dict):
        raise ValueError(f"Invalid opencode config {path}: 'server' must be a mapping.")
    extra = set(data.keys()) - {"username", "password"}
    if extra:
        raise ValueError(f"Invalid opencode config {path}: unexpected 'server' keys: {sorted(extra)}")
    username = data.get("username", "opencode")
    if not isinstance(username, str) or not username.strip():
        raise ValueError(f"Invalid opencode config {path}: 'server.username' must be a non-empty string.")
    password = data.get("password", None)
    if password is not None and not isinstance(password, str):
        raise ValueError(f"Invalid opencode config {path}: 'server.password' must be a string or null.")
    if isinstance(password, str) and password == "":
        password = None
    return OpencodeServerConfig(username=username, password=password)


def parse_opencode_user_config(path: Path) -> OpencodeUserConfig:
    """Parses ~/.agent-sandbox/opencode/config.yaml exactly once into a frozen object."""
    if not path.is_file():
        return OpencodeUserConfig()
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if data is None:
        return OpencodeUserConfig()
    if not isinstance(data, dict):
        raise ValueError(f"Invalid opencode config {path}: top level must be a mapping.")
    extra = set(data.keys()) - {"server"}
    if extra:
        raise ValueError(f"Invalid opencode config {path}: unexpected keys: {sorted(extra)}")
    return OpencodeUserConfig(server=_parse_server(data.get("server"), path))


class OpencodeAdapter(EngineAdapter):
    name = "opencode"

    def default_config(self) -> OpencodeUserConfig:
        return OpencodeUserConfig()

    def parse_user_config(self, path: Path) -> OpencodeUserConfig:
        return parse_opencode_user_config(path)

    def config_path(self, sandbox_dir: Path) -> Path:
        return sandbox_dir / self.name / CONFIG_FILENAME

    def scaffold(self, sandbox_dir: Path, repo_root: Path) -> None:
        target = self.config_path(sandbox_dir)
        if target.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        example = repo_root / EXAMPLE_REL
        if not example.is_file():
            raise FileNotFoundError(f"OpenCode config example missing: {example}")
        shutil.copyfile(example, target)

    def compose_env(self, config: object) -> tuple[str, ...]:
        if not isinstance(config, OpencodeUserConfig):
            raise TypeError(f"OpencodeAdapter.compose_env expects OpencodeUserConfig, got {type(config).__name__}")
        env: list[str] = [f"OPENCODE_SERVER_USERNAME={config.server.username}"]
        if config.server.password:
            env.append(f"OPENCODE_SERVER_PASSWORD={config.server.password}")
        return tuple(env)

    def status_lines(self, config: object) -> tuple[str, ...]:
        if not isinstance(config, OpencodeUserConfig):
            raise TypeError(f"OpencodeAdapter.status_lines expects OpencodeUserConfig, got {type(config).__name__}")
        password = config.server.password if config.server.password else "(generated - see docker logs)"
        return (
            f"username: {config.server.username}",
            f"password: {password}",
        )
