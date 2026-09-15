"""Rules compiler for engine-specific rules aggregation and shadow-mount generation."""
from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple, Sequence
from ..config.models import EngineManifest


class RuleSourceInfo(NamedTuple):
    category: str
    source_path: str
    description: str


class CompiledRulesResult(NamedTuple):
    engine_name: str
    target_container_file: str
    host_compiled_file: str
    sources: list[RuleSourceInfo]
    content: str


class RulesCompiler:
    """Discovers and compiles markdown rule layers for an engine in strict precedence."""

    def __init__(self, repo_root: str | Path, sandbox_dir: str | Path = "~/.agent-sandbox") -> None:
        self.repo_root = Path(repo_root).resolve()
        self.sandbox_dir = Path(sandbox_dir).expanduser().resolve()

    def _collect_directory_rules(self, directory: Path, category: str) -> list[tuple[RuleSourceInfo, str]]:
        if not directory.is_dir():
            return []
        collected = []
        for file_path in sorted(directory.iterdir()):
            if file_path.is_file() and not file_path.name.startswith(".") and file_path.suffix.lower() == ".md":
                content = file_path.read_text(encoding="utf-8").strip()
                if content:
                    info = RuleSourceInfo(category=category, source_path=str(file_path), description=file_path.name)
                    collected.append((info, content))
        return collected

    def _collect_host_sources(self, host_sources: Sequence[str]) -> list[tuple[RuleSourceInfo, str]]:
        collected = []
        for source in host_sources:
            path = Path(source).expanduser().resolve()
            if path.is_file():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    info = RuleSourceInfo(category="Host Global", source_path=str(path), description=path.name)
                    collected.append((info, content))
        return collected

    def compile_for_engine(self, engine: EngineManifest) -> CompiledRulesResult:
        if not engine.rules or not engine.rules.target_file:
            return CompiledRulesResult(
                engine_name=engine.name,
                target_container_file="",
                host_compiled_file="",
                sources=[],
                content="",
            )

        # Precedence tiers:
        # 1. Built-in common container rules (repo customizations/rules/*.md)
        # 2. Common user sandbox rules (~/.agent-sandbox/common/rules/*.md)
        # 3. Built-in engine rules (repo engines/<engine>/rules/*.md)
        # 4. Engine user sandbox rules (~/.agent-sandbox/<engine>/rules/*.md)
        # 5. Host global rule sources declared in manifest
        rule_layers = [
            (self.repo_root / "customizations" / "rules", "Built-in"),
            (self.sandbox_dir / "common" / "rules", "User Common"),
            (self.repo_root / "engines" / engine.name / "rules", f"Built-in {engine.name}"),
            (self.sandbox_dir / engine.name / "rules", f"User {engine.name}"),
        ]

        sources: list[RuleSourceInfo] = []
        sections: list[str] = []

        for directory, category in rule_layers:
            for info, text in self._collect_directory_rules(directory, category):
                sources.append(info)
                sections.append(f"<!-- {category.upper()} RULE: {info.description} -->\n{text}")

        for info, text in self._collect_host_sources(engine.rules.host_sources):
            sources.append(info)
            sections.append(f"<!-- HOST GLOBAL RULES: {info.description} -->\n{text}")

        final_content = ("\n\n---\n\n".join(sections) + "\n") if sections else f"# {engine.name.capitalize()} Rules\n"

        target_name = Path(engine.rules.target_file).name
        host_compiled_file = self.sandbox_dir / engine.name / target_name
        host_compiled_file.parent.mkdir(parents=True, exist_ok=True)
        host_compiled_file.write_text(final_content, encoding="utf-8")

        return CompiledRulesResult(
            engine_name=engine.name,
            target_container_file=engine.rules.target_file,
            host_compiled_file=str(host_compiled_file),
            sources=sources,
            content=final_content,
        )


def compile_rules_for_engine(
    engine: EngineManifest,
    repo_root: str | Path,
    sandbox_dir: str | Path = "~/.agent-sandbox",
) -> CompiledRulesResult:
    """Convenience function maintaining backwards compatibility."""
    return RulesCompiler(repo_root=repo_root, sandbox_dir=sandbox_dir).compile_for_engine(engine)
