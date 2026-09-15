#!/usr/bin/env bash
set -euo pipefail

TARGET_ARCH="${TARGETARCH:-}"
if [ -z "$TARGET_ARCH" ]; then
    RAW_ARCH="$(uname -m)"
    case "$RAW_ARCH" in
        x86_64|amd64)
            TARGET_ARCH="amd64"
            ;;
        aarch64|arm64)
            TARGET_ARCH="arm64"
            ;;
        *)
            echo "[OpenCode Error] Unsupported architecture: $RAW_ARCH" >&2
            exit 1
            ;;
    esac
fi

if [ "$TARGET_ARCH" = "amd64" ]; then
    OPENCODE_ARCH="x64"
else
    OPENCODE_ARCH="arm64"
fi

URL="https://github.com/anomalyco/opencode/releases/latest/download/opencode-linux-${OPENCODE_ARCH}.tar.gz"
echo "[OpenCode] Installing OpenCode ($OPENCODE_ARCH) from $URL..."

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

curl -fsSL "$URL" -o "$TEMP_DIR/opencode.tar.gz"
tar -xzf "$TEMP_DIR/opencode.tar.gz" -C "$TEMP_DIR"

FOUND_BIN="$(find "$TEMP_DIR" -type f -name "opencode" | head -n 1)"
if [ -z "$FOUND_BIN" ]; then
    FOUND_BIN="$(find "$TEMP_DIR" -type f -exec grep -lI '^.ELF' {} + | head -n 1)"
fi

if [ -z "$FOUND_BIN" ]; then
    echo "[OpenCode Error] opencode binary not found in extracted archive" >&2
    exit 1
fi

mkdir -p /usr/local/bin
cp "$FOUND_BIN" /usr/local/bin/opencode
chmod +x /usr/local/bin/opencode
echo "[OpenCode] Installed /usr/local/bin/opencode successfully."
