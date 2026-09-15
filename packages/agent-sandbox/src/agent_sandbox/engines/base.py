"""Generic engine-adapter contract. No engine-specific names or logic here."""
from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class EngineAdapter(Protocol):
    """Per-engine adapter. Generic core interacts only through this interface."""

    name: str

    def default_config(self) -> object:
        """Returns the engine's default user config (typed per engine)."""
        ...

    def parse_user_config(self, path: Path) -> object:
        """Parses and validates the user config file exactly once.

        Missing file must return the default. Invalid content must raise
        ValueError with the file path in the message. Raw mappings must not
        escape; callers receive only the engine's frozen config object.
        """
        ...

    def scaffold(self, sandbox_dir: Path, repo_root: Path) -> None:
        """Creates the user config file from the tracked example if missing."""
        ...

    def compose_env(self, config: object) -> tuple[str, ...]:
        """Returns extra container environment entries for this engine."""
        ...

    def status_lines(self, config: object) -> tuple[str, ...]:
        """Returns human-readable status lines. Zero redaction: values shown."""
        ...

    def legacy_container_names(self) -> tuple[str, ...]:
        """Legacy Docker container names that map to this engine (generic migration)."""
        return ()

    def run_legacy_migrations(self) -> None:
        """Runs engine-owned legacy state migrations (generic hook, no-op by default)."""
        return None
