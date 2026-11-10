# Antigravity Command Security Policy (`BypassSandbox`)

## 1. Two-Tier Sandbox Architecture
Inside this container, Antigravity still operates its inner command security policy:
- `BypassSandbox: false` (default): Disables container network access and restricts writes to workspace directories to allow safe commands to auto-run without prompting the user.
- `BypassSandbox: true`: Enables network access and container root filesystem permissions for that command, prompting the user for approval.

## 2. Using `BypassSandbox` Inside the Container
- **`BypassSandbox: true` NEVER breaks out of the Docker container.** It cannot compromise the developer's macOS host.
- **When to use `BypassSandbox: false` (Default)**: Use for offline, workspace-local operations (running local tests, reading/editing code, compiling with pre-installed compilers, git commands, offline builds).
- **When to use `BypassSandbox: true`**: Use whenever a command needs internet/network access (e.g., `sandbox-pkg get <package>`, `npm install`, `pip install`, `cargo build` pulling crates, `curl`, `wget`) or needs to modify container system paths outside the workspace (e.g., `/etc`, `/usr`, `/var`).
  - Package installs do not need root: prefer `sandbox-pkg get <package>`, `npm install -g`, and `pip install --user`, all of which write to the persistent home volume. Do not reach for `sudo`.
