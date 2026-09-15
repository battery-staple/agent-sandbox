# Agent Container Execution Environment

You are operating inside a secure, isolated Linux container (Ubuntu 24.04) running in Docker/OrbStack on behalf of the developer.

## 1. Container Sandbox Architecture & Isolation
- **Container Isolation**: Your entire execution environment is contained inside an isolated Linux container.
- **Host System Protection**: Host macOS system files, `~/.ssh`, `~/.aws`, Keychain, and files outside whitelisted workspaces are physically isolated and inaccessible directly.
- **Path Parity**: Whitelisted workspaces are mounted directly to their host absolute paths (e.g. `/Users/...`), maintaining full path parity across host and container.
- **Persistent State**: Your home directory `/home/developer` resides on an isolated persistent volume preserving dotfiles, package caches, and agent state across container restarts.

## 2. macOS Host Binary Execution (`host-exec`)
- Because you are operating inside an isolated Linux container, macOS-native host binaries cannot be executed directly via Linux shell commands.
- Whenever you need to invoke macOS host tools or system utilities, use the `host-exec` bridge tool (`host-exec <command> [args...]`).
- **Discovering Permitted Host Commands**: To inspect the currently active whitelist of host tools, permitted argument patterns, and approval policies, consult the `host-exec` skill or run `host-exec --list`.
- **Host Bridge Offline Remediation**: If `host-exec` returns `[HOST-EXEC ERROR] Host Bridge Daemon is not running on the macOS host`, do not retry in a loop. Ask the user to start the host bridge by running `agent-sandbox host-bridge` (or `./bin/agent-sandbox host-bridge`) on their macOS host. Once the user confirms it is running, retry the command.
