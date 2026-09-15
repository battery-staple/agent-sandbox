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
            echo "[Antigravity Error] Unsupported architecture: $RAW_ARCH" >&2
            exit 1
            ;;
    esac
fi

if [ "$TARGET_ARCH" = "amd64" ]; then
    LS_URL="https://storage.googleapis.com/antigravity-public/antigravity-hub/2.8.1-6512087774658560/linux-x64/Antigravity.tar.gz"
else
    LS_URL="https://storage.googleapis.com/antigravity-public/antigravity-hub/2.8.1-6512087774658560/linux-arm/Antigravity.tar.gz"
fi

echo "[Antigravity] Installing Language Server ($TARGET_ARCH) from $LS_URL..."

TEMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TEMP_DIR"' EXIT

curl -fsSL "$LS_URL" -o "$TEMP_DIR/Antigravity.tar.gz"
tar -xzf "$TEMP_DIR/Antigravity.tar.gz" -C "$TEMP_DIR"

FOUND_BIN="$(find "$TEMP_DIR" -type f \( -name "language_server" -o -name "language_server_linux_arm" -o -name "language_server_linux_x64" -o -name "language_server_linux_arm64" \) | head -n 1)"
if [ -z "$FOUND_BIN" ]; then
    FOUND_BIN="$(find "$TEMP_DIR" -type f -exec grep -lI '^.ELF' {} + | head -n 1)"
fi

if [ -z "$FOUND_BIN" ]; then
    echo "[Antigravity Error] language_server binary not found in extracted archive" >&2
    exit 1
fi

mkdir -p /usr/local/bin
cp "$FOUND_BIN" /usr/local/bin/language_server
chmod +x /usr/local/bin/language_server
echo "[Antigravity] Installed /usr/local/bin/language_server successfully."
