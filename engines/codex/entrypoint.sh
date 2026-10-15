#!/usr/bin/env bash
set -e

# Runs as root: the guest entrypoint's init phase has already created and
# chowned the persistent home dirs, and sshd must run as root (it drops to the
# developer user per session). No sudo/chown is needed here.

# Trust the agent-sandbox public key (mounted read-only by the compose override).
AUTHORIZED_KEYS_SRC=/etc/sandbox/ssh/authorized_keys
if [ -f "$AUTHORIZED_KEYS_SRC" ]; then
  SANDBOX_GID="$(id -g developer 2>/dev/null || echo 1000)"
  install -d -o developer -g "$SANDBOX_GID" -m 700 "$HOME/.ssh" 2>/dev/null || true
  if ! install -o developer -g "$SANDBOX_GID" -m 600 "$AUTHORIZED_KEYS_SRC" "$HOME/.ssh/authorized_keys"; then
    echo "[Codex Warning] could not install authorized_keys" >&2
    ls -ld "$AUTHORIZED_KEYS_SRC" "$HOME" "$HOME/.ssh" >&2 || true
  fi
fi

# Seed the container config once (never clobber user edits).
if [ ! -f "$HOME/.codex/config.toml" ] && [ -f /etc/sandbox/engines/codex/config.toml.example ]; then
  install -d -o developer -g developer "$HOME/.codex" 2>/dev/null || true
  install -o developer -g developer -m 644 /etc/sandbox/engines/codex/config.toml.example "$HOME/.codex/config.toml" 2>/dev/null \
    || echo "[Codex Warning] could not seed config.toml" >&2
fi

# Drop stale app-server sockets from a previous container life.
rm -f "$HOME/.codex/app-server-control"/*.sock 2>/dev/null || true

echo "=========================================================="
echo "  Starting Codex Sandboxed Runtime (SSH remote frontend)"
echo "  SSH Port        : 58434 (published on 127.0.0.1)"
echo "  Web UI          : none (use the Codex desktop app)"
echo "=========================================================="

# Persist sshd host keys on the home volume so the desktop app's known_hosts
# stays valid across container recreation (ephemeral /etc/ssh keys cause
# "REMOTE HOST IDENTIFICATION HAS CHANGED").
HOSTKEY_DIR=/home/developer/.sshd
install -d -m 700 "$HOSTKEY_DIR"
for hostkey_type in ed25519 rsa ecdsa; do
  hostkey="$HOSTKEY_DIR/ssh_host_${hostkey_type}_key"
  [ -f "$hostkey" ] || ssh-keygen -q -t "$hostkey_type" -N "" -f "$hostkey"
done

# /run/sshd: privilege-separation dir; /var/empty: sshd's privsep chroot target.
mkdir -p /run/sshd /var/empty
exec /usr/sbin/sshd -D -e
