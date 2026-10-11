# Static detection analysis: untrusted catalog path -> Docker bind mount

## Defect (one line)
- `skills/catalog.py` `parse_catalogs` reads agent-writable JSON (`json.load`, `entries[].path`), resolves to host paths; `compose/generator.py` emits `volumes.append(f"{c_path}:{c_path}:ro")` with no containment check.
- Fix at HEAD: `_allowed_realpath` (single `realpath` + prefix check vs host-only roots) + `filter_allowed_roots`, wired into `resolve_for_engine`; generator passes `allowed_skill_roots`/`sandbox_dir`.

## Techniques considered
- **Semgrep taint mode** (`json.load` -> `volumes.append` with sanitizer `realpath`+`startswith`): most expressive standard option; rule would be ~20 lines and cross-file capable. Rejected for PoC: `semgrep` binary not installed and install is not possible in this offline sandbox (`semgrep: command not found`; `pip install semgrep` timed out with no network).
- **CodeQL**: full interprocedural taint tracking (`UntrustedDataToExternalApi`-style custom query). Unrealistic here: no CodeQL DB/CLI in repo, heavyweight for a small Python repo with pytest+Actions CI, overkill for one known sink.
- **Custom AST/dataflow script (chosen)**: stdlib `ast` check on the two known files — source/sink are fixed locations, guard has a distinctive shape (`realpath` + `startswith` + allowlist roots). Zero dependencies, ms runtime, fits existing CI.
- **Plain grep lint**: considered, rejected as primary: `grep -rn 'c_path}:{c_path}'` finds the sink but cannot tell guarded from unguarded; AST check of guard presence/wiring is barely more code and gives the fixed=clean property.

## Artifact
- `tools/check_untrusted_mounts.py` (stdlib only). Logic:
  - sink: `generator.py` contains `resolve_for_engine`/`catalog_skill*` reference AND a `volumes.append(...)` over `c_path`/`catalog` (AST + `{c_path}` fallback).
  - guard: reachability analysis over `CatalogSkillResolver` methods + module-level helpers in `catalog.py`. A helper is a containment guard if it uses `realpath` AND uses `startswith` itself or transitively reaches one that does (fixpoint). Entry points are `resolve_for_engine` plus `resolve_with_rejections` when present; reachability follows `self.<method>()` and bare calls transitively.
  - verdict: sink present AND no containment guard reachable from an entry point -> exit 1 (FAIL); else exit 0 (PASS). Unparseable files fail closed.
- CI: `.github/workflows/static-guards.yml` runs `python tools/check_untrusted_mounts.py` (no pip deps).

## Evidence (exact commands, run from repo root)
- Fixed HEAD (expect exit 0):
  - `python3 tools/check_untrusted_mounts.py`
  - output: `guard: OK (containment guard reachable from entry point: _allowed_realpath ...)`, `sink: present (...)`, `PASS: catalog-derived mount source is containment-guarded`, `exit=0`
- Sibling refactors (each expect exit 0 via `--catalog <wt>/.../catalog.py --generator <wt>/.../generator.py`):
  - `wt-catalog-quality` (`_allowed_realpath` delegates prefix check to module helper `_is_within`): guard OK via transitive `realpath` + reached `startswith`.
  - `wt-catalog-ux` (`resolve_for_engine` delegates to `resolve_with_rejections`): guard OK via entry-point closure over both names.
  - `wt-catalog-hardening` (bounded `parse_catalogs`, `_warn_agent_writable_roots`): guard OK, shape unchanged.
- Vulnerable origin/main (expect exit 1):
  - `git worktree add /tmp/vuln origin/main`
  - `python3 tools/check_untrusted_mounts.py --catalog /tmp/vuln/packages/agent-sandbox/src/agent_sandbox/skills/catalog.py --generator /tmp/vuln/packages/agent-sandbox/src/agent_sandbox/compose/generator.py`
  - output: `guard: MISSING (missing guard: _allowed_realpath(realpath+startswith), filter_allowed_roots->_allowed_realpath, resolve_for_engine->filter_allowed_roots/_allowed_realpath)`, `FAIL: host path derived from JSON catalog content reaches a Docker bind-mount source without a containment/allowlist guard`, `exit=1`
  - cleanup: `git worktree remove --force /tmp/vuln`
- Mutation (guard defined but unwired still flags, expect exit 1):
  - copy fixed `catalog.py`, replace `return self.filter_allowed_roots(...)` with `return self.filter_conflicts(valid_paths)`, rerun with `--catalog <copy>` -> `guard: MISSING (missing guard: containment helper(s) _allowed_realpath not reachable from resolve_for_engine)`, `exit=1`
- Suite: `python3 -m pytest packages/agent-sandbox/tests/ -q -p no:cacheprovider` -> `133 passed`. Full `python3 -m pytest packages/ -q` also run per task (host-exec concurrency tests make it slow; see commit-test output).

## Recommendation
- Keep the checker + CI step: precise on vuln-vs-fixed, fast, zero-dep, low-noise (only watches the two files/patterns it understands).
- Do not present it as general taint analysis; it is a narrow regression guard for this specific sink.

## Limitations (do not overstate)
- Not a real taint engine: hardcoded to `catalog.py`/`generator.py`, entry points `resolve_for_engine`/`resolve_with_rejections`, sink idiom `volumes.append(...c_path...)`. A renamed entry point or a second sink (e.g. new mount site, `engine.mounts`, rules target) is missed until the checker is extended.
- Guard predicate is syntactic, not semantic: any reachable `realpath` + (transitively reached) `startswith` counts, even a coincidental pairing (e.g. `realpath` for cycle detection plus a `"~"`-prefix check in the same function). Correctness of the root set and single-resolution discipline is only covered by unit tests (`test_skills.py`, `test_generator.py`), not by this check.
- No dataflow across helpers: inlining resolution into another module, or passing catalog paths through a generic list, evades it.
- Fail-closed on parse errors: a syntax-broken file reports FAIL, which is intentional but noisy during large refactors.
- If the repo later adopts Semgrep/CodeQL, replace this with a proper taint rule (`json.load` source, bind-mount sink, `realpath`-containment sanitizer) and keep this script only as a fast pre-commit fallback.
