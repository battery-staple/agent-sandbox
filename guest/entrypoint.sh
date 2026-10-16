#!/usr/bin/env bash
set -e

ENGINE="${ENGINE:-${SANDBOX_ENGINE:-antigravity}}"
SANDBOX_USER="${SANDBOX_USER:-developer}"
SANDBOX_HOME="${SANDBOX_HOME:-/home/developer}"
export HOME="${SANDBOX_HOME}"
export TMPDIR="${TMPDIR:-${SANDBOX_HOME}/.sandbox-tmp}"

# The container starts as root so this init phase can repair ownership of the
# persistent home volume (which may be root-owned or carry a stale UID) before
# handing off to the engine. Engine entrypoints drop privileges themselves;
# sshd-based engines stay root.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$TMPDIR" "$SANDBOX_HOME/.npm-global" "$SANDBOX_HOME/.gradle" \
             "$SANDBOX_HOME/go/bin" "$SANDBOX_HOME/.codex" "$SANDBOX_HOME/.agents" \
             "$SANDBOX_HOME/.ssh" /workspace 2>/dev/null || true
    chown "$SANDBOX_USER:$SANDBOX_USER" "$SANDBOX_HOME" 2>/dev/null || true
    # Scoped + idempotent: only wrong-owner entries are chowned, only under the
    # dirs we manage (never a blind `chown -R` over large caches).
    for d in "$TMPDIR" "$SANDBOX_HOME/.npm-global" "$SANDBOX_HOME/.gradle" \
             "$SANDBOX_HOME/go" "$SANDBOX_HOME/.codex" "$SANDBOX_HOME/.agents" \
             "$SANDBOX_HOME/.ssh" "$SANDBOX_HOME/.config"; do
        [ -e "$d" ] || continue
        find "$d" \! -user "$SANDBOX_USER" -exec chown "$SANDBOX_USER:$SANDBOX_USER" {} + 2>/dev/null || true
    done
fi

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
    if [ "$(id -u)" = "0" ]; then
        chown -R "$SANDBOX_USER:$SANDBOX_USER" "$SANDBOX_SKILLS_TARGET" 2>/dev/null || true
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
