"""Engine adapter registry and parse-once runtime bundle.

Generic core resolves adapters by engine name and holds typed configs opaquely.
Per-engine shapes live only in their adapter modules.
"""
from __future__ import annotations

from dataclasses import dataclass
from os.path import abspath, expanduser
from pathlib import Path
from typing import Sequence

from ..config.models import EngineManifest
from .antigravity import AntigravityAdapter
from .base import EngineAdapter
from .opencode import OpencodeAdapter


@dataclass(frozen=True)
class EngineRuntime:
    manifest: EngineManifest
    adapter: EngineAdapter
    config: object


_ADAPTERS: dict[str, EngineAdapter] = {
    OpencodeAdapter.name: OpencodeAdapter(),
    AntigravityAdapter.name: AntigravityAdapter(),
}


CONTAINER_NAME_PREFIX = "agent-sandbox-"


def resolve_engine_name(container_name: str) -> str | None:
    """Maps a Docker container name to its engine, or None if unrelated.

    Handles the canonical '<prefix><engine>' names plus each adapter's
    legacy names (e.g. pre-rename 'antigravity-sandbox').
    """
    name = container_name.strip()
    if name.startswith(CONTAINER_NAME_PREFIX):
        return name[len(CONTAINER_NAME_PREFIX):]
    return next(
        (adapter.name for adapter in all_adapters() if name in adapter.legacy_container_names()),
        None,
    )


def get_adapter(name: str) -> EngineAdapter:
    if name not in _ADAPTERS:
        raise KeyError(f"Unknown engine adapter '{name}'. Available: {sorted(_ADAPTERS.keys())}")
    return _ADAPTERS[name]


def all_adapters() -> tuple[EngineAdapter, ...]:
    return tuple(_ADAPTERS[name] for name in sorted(_ADAPTERS.keys()))


def scaffold_all(sandbox_dir: str | Path, repo_root: str | Path) -> None:
    """Idempotently scaffolds every known engine's user config file."""
    sb = Path(os_expand(sandbox_dir))
    rr = Path(os_expand(repo_root))
    for adapter in all_adapters():
        adapter.scaffold(sb, rr)


def os_expand(p: str | Path) -> str:
    return abspath(expanduser(str(p)))


def _load_runtime(sandbox_dir: Path, manifest: EngineManifest) -> EngineRuntime:
    adapter = get_adapter(manifest.name)
    config = adapter.parse_user_config(sandbox_dir / manifest.name / "config.yaml")
    return EngineRuntime(manifest=manifest, adapter=adapter, config=config)


def load_all_runtimes(
    manifests: Sequence[EngineManifest],
    sandbox_dir: str | Path,
) -> tuple[EngineRuntime, ...]:
    """Parses each engine's user config exactly once. Fail fast on invalid content."""
    sb = Path(os_expand(sandbox_dir))
    return tuple(_load_runtime(sb, manifest) for manifest in manifests)


def get_runtime(
    runtimes: Sequence[EngineRuntime],
    name: str,
) -> EngineRuntime:
    """Returns the runtime for an engine name. Raises KeyError when absent."""
    try:
        return next(runtime for runtime in runtimes if runtime.manifest.name == name)
    except StopIteration:
        raise KeyError(f"No runtime loaded for engine '{name}'.") from None


def runtimes_for_active(
    active_manifests: Sequence[EngineManifest],
    all_runtimes: Sequence[EngineRuntime],
) -> tuple[EngineRuntime, ...]:
    """Filters parse-once runtimes to the active set without re-parsing files."""
    return tuple(get_runtime(all_runtimes, m.name) for m in active_manifests)
