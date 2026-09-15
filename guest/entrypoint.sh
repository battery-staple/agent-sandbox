#!/usr/bin/env bash
set -e

ENGINE="${ENGINE:-${SANDBOX_ENGINE:-antigravity}}"
export TMPDIR="${TMPDIR:-/home/developer/.sandbox-tmp}"

mkdir -p "$TMPDIR" \
         /home/developer/.npm-global \
         /home/developer/.gradle \
         /home/developer/go/bin \
         /workspace 2>/dev/null || true

# Seed host-exec skill into engine skills target if defined
if [ -n "${SANDBOX_SKILLS_TARGET:-}" ]; then
    mkdir -p "$SANDBOX_SKILLS_TARGET/host-exec" 2>/dev/null || true
    CUSTOM_SKILL_SRC="/etc/sandbox/customizations/skills/host-exec/SKILL.md"
    if [ ! -f "$CUSTOM_SKILL_SRC" ]; then
        CUSTOM_SKILL_SRC="/etc/antigravity/customizations/skills/host-exec/SKILL.md"
    fi
    if [ -f "$CUSTOM_SKILL_SRC" ]; then
        cp -u "$CUSTOM_SKILL_SRC" "$SANDBOX_SKILLS_TARGET/host-exec/" 2>/dev/null || \
        cp "$CUSTOM_SKILL_SRC" "$SANDBOX_SKILLS_TARGET/host-exec/" 2>/dev/null || true
    fi
fi

ENGINE_ENTRYPOINT="/etc/sandbox/engines/$ENGINE/entrypoint.sh"
if [ -f "$ENGINE_ENTRYPOINT" ]; then
    chmod +x "$ENGINE_ENTRYPOINT" 2>/dev/null || true
    exec "$ENGINE_ENTRYPOINT" "$@"
else
    echo "[Sandbox Error] Engine '$ENGINE' entrypoint not found at $ENGINE_ENTRYPOINT" >&2
    if [ "$#" -gt 0 ]; then
        exec "$@"
    else
        exit 1
    fi
fi
