---
name: host-exec
description: Use this skill to inspect and execute whitelisted macOS host binaries from inside the Linux container sandbox using the host-exec bridge tool. Run `host-exec --list` to view all permitted host tools and policies.
---

# Host-Exec Skill (Host Binary Execution Bridge)

Use this skill when you need to invoke tools or commands that only exist on the developer's macOS host machine (outside the Linux container).

## 1. When to Use `host-exec`

- **Standard Linux / Dev Tools** (Node, Python, Go, Rust, Git, gcc, make, bash, cargo, npm, pip): **Run directly in the container**.
- **macOS-Specific Host Tools** (Xcode, Apple SDKs, iOS Simulator, macOS Keychain, macOS system utilities): **Use `host-exec`**.

### Discovering Permitted Host Tools Live

To inspect the current whitelist of available macOS tools, allowed argument patterns, descriptions, and approval requirements, run:

```bash
host-exec --list
```

---

## 2. Command Syntax & Usage

Run the `host-exec` CLI tool followed by the host command and its arguments:

```bash
host-exec <command> [args...]
```

### Examples

```bash
# List all whitelisted host commands and policies
host-exec --list

# Execute a whitelisted host command
host-exec sw_vers

# Run Xcode build tool on host
host-exec xcodebuild -showsdks

# Launch macOS iOS Simulator
host-exec open -a Simulator
```

---

## 3. How the Bridge Works Under the Hood

1. **Single Source of Truth (`~/.agent-sandbox/whitelist.yaml`)**:
   - The host daemon dynamically reads security policies from `~/.agent-sandbox/whitelist.yaml` on the host machine.
   - Any modifications made by the user to the whitelist file take effect immediately without restarting daemons or containers.
2. **Path Translation**:
   - When you execute `host-exec` from a whitelisted workspace directory, the host bridge automatically matches the container path with the host filesystem path.
3. **HMAC-SHA256 Authentication**:
   - `host-exec` signs every request with a secret token shared between the host daemon and the container.
4. **Interactive User Approval**:
   - If a command is configured with `require_interactive_approval: true`, a native macOS dialog will appear on the developer's screen with **Approve** / **Deny** buttons.
   - *Note*: If the command is waiting for user approval, do not spam repeated executions.

---

## 4. Handling Host Bridge Errors & Remediation

### Scenario A: Host Bridge Daemon Is Not Running

If a command fails with an error indicating the host bridge daemon is unreachable:

```text
[HOST-EXEC ERROR] Host Bridge Daemon is not running on the macOS host
```

**Guidance for Agent**:

1. **Do not retry immediately**: The command cannot succeed while the daemon is offline.
2. **Inform the user clearly**: Explain that the requested tool requires execution on the macOS host, but the host bridge daemon is not running.
3. **Provide the exact remediation command**: Ask the user to start the daemon in their macOS terminal:

   ```bash
   agent-sandbox host-bridge
   ```

   *(or `./bin/agent-sandbox host-bridge` from the repository directory)*
4. **Wait for user confirmation**: Once the user confirms the host bridge is active, retry the command.

---

## 5. Handling Whitelist Rejections & Approvals

### Scenario B: Whitelist Policy Rejection

If a command fails with a whitelist rejection error, `host-exec` will automatically display the list of currently permitted commands and argument patterns.

**Guidance for Agent**:

1. Review the output of `host-exec --list` to check if an alternative whitelisted command or argument format is available.
2. If the tool is not whitelisted, inform the user and direct them to add the command to `allowed_commands` in `~/.agent-sandbox/whitelist.yaml` on their macOS host.

### Scenario C: Interactive User Approval Denied

If a command fails with:
`[HOST-EXEC ERROR] Host Execution Failed: Execution denied by user via native approval dialog`

**Guidance for Agent**:

1. The user explicitly chose **Deny** on the native macOS approval dialog.
2. Do not retry the command without user instruction. Respect the user's decision and ask how they'd like to proceed.
