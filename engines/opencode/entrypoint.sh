#!/usr/bin/env bash
set -e

install -d -o developer -g developer "$HOME/.config/opencode" 2>/dev/null || true

echo "=========================================================="
echo "  Starting OpenCode Sandboxed Runtime Container"
echo "  Web UI Port     : 4096 (0.0.0.0)"
echo "  Workspace Mount : /workspace"
echo "=========================================================="

exec setpriv --reuid=developer --regid=developer --init-groups opencode serve --hostname 0.0.0.0 --port 4096 "$@"
