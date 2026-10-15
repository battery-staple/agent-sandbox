#!/usr/bin/env bash
set -euo pipefail

# Installs the Codex CLI via npm: @openai/codex
# Docs: https://github.com/openai/codex

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y --no-install-recommends openssh-server netcat-openbsd
rm -rf /var/lib/apt/lists/*

CODEX_VERSION="${CODEX_VERSION:-0.162.1}"
CODEX_PACKAGE="${CODEX_PACKAGE:-@openai/codex}"

if ! command -v node >/dev/null 2>&1; then
    echo "[Codex Error] node is required but not found in PATH." >&2
    exit 1
fi

if ! command -v npm >/dev/null 2>&1; then
    echo "[Codex Error] npm is required but not found in PATH." >&2
    exit 1
fi

echo "[Codex] Installing Codex (${CODEX_PACKAGE}@${CODEX_VERSION}) via npm..."
npm install -g --allow-scripts="${CODEX_PACKAGE}" "${CODEX_PACKAGE}@${CODEX_VERSION}"

if ! command -v codex >/dev/null 2>&1; then
    echo "[Codex Error] codex binary not found after npm install." >&2
    exit 1
fi

# Pin into /usr/local/bin so a NON-INTERACTIVE login shell (the desktop app's
# SSH flow) resolves codex even when the npm global bin dir is off PATH.
ln -sf "$(command -v codex)" /usr/local/bin/codex

codex --version
echo "[Codex] Installed /usr/local/bin/codex successfully."

# sshd configuration for the remote-connection frontend.
install -d /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/codex.conf <<'SSHD_EOF'
Port 58434
ListenAddress 0.0.0.0
HostKey /home/developer/.sshd/ssh_host_ed25519_key
HostKey /home/developer/.sshd/ssh_host_rsa_key
HostKey /home/developer/.sshd/ssh_host_ecdsa_key
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
AllowUsers developer
AllowTcpForwarding local
X11Forwarding no
PrintMotd no
PrintLastLog no
UsePAM no
StrictModes no
Subsystem sftp internal-sftp
SSHD_EOF

# Login shells stay quiet by construction: the Ubuntu skeleton .bashrc/.profile
# guard non-interactive shells, and sshd_config.d/codex.conf disables MOTD and
# last-login output. The desktop app parses remote stdout during SSH bootstrap,
# so no extra .bashrc munging is needed.
