#!/usr/bin/env bash
set -e

mkdir -p "$HOME/.config/opencode" 2>/dev/null || true

echo "=========================================================="
echo "  Starting OpenCode Sandboxed Runtime Container"
echo "  Web UI Port     : 4096 (0.0.0.0)"
echo "  Workspace Mount : /workspace"
echo "=========================================================="

exec opencode web --hostname 0.0.0.0 --port 4096 "$@"
