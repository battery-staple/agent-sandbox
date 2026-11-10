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

## 3. Browser Subagent & Automated Browser Capability
- The container environment includes an automated browser dispatcher (`google-chrome`, `chromium`, `chromium-browser`) supporting dual execution modes for browser subagents and automation tools:
  - **Host Interactive Mode**: When `agent-sandbox host-bridge` is active, browser requests drive native Google Chrome visibly on the macOS desktop via `host-exec chrome` and transparent loopback CDP forwarding.
  - **Container Headless Mode**: Fully isolated offscreen Chromium running inside the container with `--no-sandbox`.
  - **Auto Mode (Default)**: Automatically uses host Chrome if the host bridge is running, and gracefully falls back to in-container Chromium if offline.
- **Configuration**: Users can configure the browser mode in `~/.agent-sandbox/antigravity/config.yaml`:
  ```yaml
  browser_mode: auto  # auto | host | container
  ```

## 4. Installing Packages (No Root Needed)
- Your home directory `/home/developer` is a persistent volume; the rest of the container filesystem is discarded whenever the engine is recreated. Install everything under your home.
- **Language toolchains**: `npm install -g` writes to `~/.npm-global`, `pip install --user` to `~/.local`, and `go install` / `cargo install` / `gem install --user-install` to their home prefixes.
- **Distro binaries**: use `sandbox-pkg get <package>`. It downloads the Ubuntu package into a user-owned cache, extracts it under `~/.local/opt/sandbox-pkg/<package>/<version>`, and links its executables into `~/.local/bin`, which is already on `PATH`. It never runs package maintainer scripts and never needs root.
  - `sandbox-pkg list` shows what is installed; `sandbox-pkg remove <package>` uninstalls it.
  - It downloads from the Ubuntu archive, so it needs network access (see engine-specific command approval settings).
- **Avoid `sudo` / `apt-get install`**: system-layer installs do not survive container recreation, and they are outside this sandbox's rootless install policy.
- If a task genuinely needs a system-level dependency at image-build time, ask the user to add it to `Dockerfile.sandbox` and rebuild with `agent-sandbox build`.
