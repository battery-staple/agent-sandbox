#!/usr/bin/env bash
set -euo pipefail

# Installs the supported V2 distribution via npm: @opencode/cli
# Docs: https://opencode.ai/v2/docs (npm install -g @opencode/cli)

OPENCODE_VERSION="${OPENCODE_VERSION:-latest}"
OPENCODE_PACKAGE="${OPENCODE_PACKAGE:-@opencode/cli}"

if ! command -v node >/dev/null 2>&1; then
    echo "[OpenCode Error] node is required but not found in PATH." >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "[OpenCode Error] npm is required but not found in PATH." >&2
    exit 1
fi

echo "[OpenCode] Installing OpenCode V2 (${OPENCODE_PACKAGE}@${OPENCODE_VERSION}) via npm..."
npm install -g "${OPENCODE_PACKAGE}@${OPENCODE_VERSION}"

if ! command -v opencode >/dev/null 2>&1; then
    echo "[OpenCode Error] opencode binary not found after npm install." >&2
    exit 1
fi

opencode --version
echo "[OpenCode] Installed $(command -v opencode) successfully."
