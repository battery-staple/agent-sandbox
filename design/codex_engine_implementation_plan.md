# Implementation Plan: `codex` engine (stock desktop app ⇄ containerized backend over SSH)

Status: approved design, ready to implement.
Related: [`agent_sandbox_architecture.md`](./agent_sandbox_architecture.md).

## 1. Goal

Add OpenAI **Codex** as a first-class agent sandbox engine. Unlike `opencode` and
`antigravity`, Codex ships **no browser UI**, and its `codex app-server` serves
**no web assets** (verified: pure JSON-RPC over stdio/unix/ws). Its real frontend
is the proprietary Codex/ChatGPT desktop app.

We therefore adopt the **officially supported "remote connection" model** (route E):

> The `codex` engine container runs `sshd` and `codex`; the stock desktop app on
> macOS connects via **Settings → Connections → SSH**, spawns `codex app-server`
> remotely, and drives it. The container is the security boundary.

This mirrors Docker's own `sbx-kit-codex-app-server`, CoreWeave's Codex sandbox,
and the AWS "Codex app against a private dev box" guide. No asar patching, no
Electron emulation.

## 2. Decisions (locked)

| ID | Decision |
|----|----------|
| D1 | sshd inside the container; dedicated per-engine ed25519 keypair managed by agent-sandbox; a marker-wrapped `Host` block prepended into `~/.ssh/config`; per-engine `known_hosts`. |
| D2 | Container auth = **`codex login --device-auth` only** (ChatGPT subscription). No API key. Never share the host `~/.codex`. |
| D3 | Pin `CODEX_VERSION` at build; **plus** a host-vs-container version mismatch warning. Automatic app-version matching deferred. |
| D4 | Unify engine connection config: replace `web_url` with a `connection` object; migrate opencode/antigravity to `connection: {type: web, url}`. |
| D5 | Container `config.toml`: `sandbox_mode = "danger-full-access"`, `approval_policy = "on-request"`. Never `untrusted` (retired in Codex v0.149 → blocks startup). |
| D6 | Rules → `~/.codex/AGENTS.md`; skills → `~/.agents/skills`. |
| D7 | Install via pinned npm `@openai/codex@$CODEX_VERSION`, symlinked into `/usr/local/bin` so the non-interactive login shell resolves it. |
| D8 | Full first-class scope; fallback E′ (`CODEX_APP_SERVER_WS_URL`) documented but not implemented. |

## 3. Architecture

```
macOS host                                   │  Docker container "agent-sandbox-codex"
  agent-sandbox CLI (Python)                 │   entrypoint → sshd (port 58434)
  ~/.agent-sandbox/codex/ssh/  (key, config) │   ~/.codex  → named volume agent_home_codex
  ~/.ssh/config  (prepended Host block)      │     config.toml  (danger-full-access, on-request, file store)
  ChatGPT.app / Codex.app  ──ssh──────────────▶  codex app-server (spawned by the app's SSH flow)
      ▲  deep link / permissions UI          │   codex login --device-auth → auth.json (in volume)
      └─ approvals & chat rendered here      │
```

Key facts that constrain the design:

- The desktop app **does not relay** its ChatGPT credentials to the remote; the
  remote `codex` authenticates independently (`codex login --device-auth`).
- The container `$CODEX_HOME` **must** live on a container-native volume: the
  app-server creates Unix sockets (`app-server-control/*.sock`) and **UDS do not
  work over Docker Desktop/OrbStack macOS shared mounts** (docker/for-mac#483
  "Won't Fix", orbstack#62). Therefore **do not bind-mount the host `~/.codex`**.
- Version skew below the app's minimum is a **blocking error** with versions
  shown; above the minimum it is **silent** (models cache / compaction /
  feature-enablement breakages). Hence the D3 warning.

## 4. Work breakdown — atomic MRs

Dependency order: **MR1 → MR2 → MR3 → MR4**. Each is independently reviewable and
merges without leaving the tree broken.

### MR1 — Unify engine connection config (`connection`, drop `web_url`)

**Goal:** replace the required `web_url` manifest field with a required
`connection` object; migrate existing engines. No Codex yet — pure refactor.

**Files**
- `config/engine-manifest.schema.json` — remove `web_url` from `required` and
  `properties`; add:

  ```jsonc
  "connection": {
    "oneOf": [
      { "type": "object", "required": ["type", "url"],
        "properties": { "type": { "const": "web" },
                        "url": { "type": "string", "format": "uri" } },
        "additionalProperties": false },
      { "type": "object", "required": ["type", "user", "host_alias"],
        "properties": { "type": { "const": "ssh" },
                        "user": { "type": "string" },
                        "host_alias": { "type": "string" } },
        "additionalProperties": false }
    ]
  }
  ```
  Add `"connection"` to `required`.
- `packages/agent-sandbox/src/agent_sandbox/config/models.py` — add frozen
  `EngineConnectionConfig(type: str, url: str | None = None, user: str | None = None,
  host_alias: str | None = None)`; replace `EngineManifest.web_url: str` with
  `connection: EngineConnectionConfig`.
- `packages/agent-sandbox/src/agent_sandbox/config/loader.py` — parse/validate
  `connection` into `EngineConnectionConfig` (web requires `url`; ssh requires
  `user` + `host_alias`).
- `engines/opencode/manifest.yaml` → `connection: {type: web, url: http://127.0.0.1:4096}`
- `engines/antigravity/manifest.yaml` → `connection: {type: web, url: https://localhost:58432}`
- `packages/agent-sandbox/src/agent_sandbox/cli.py` — use
  `engine.connection.url` where `web_url` was used (start summary, status, ui).
  Keep behavior identical for `web`.
- Tests: update every `EngineManifest(..., web_url=...)` and `web_url` assertion:
  `test_loader.py`, `test_models.py`, `test_generator.py`, `test_cli_running.py`,
  `test_framework.py`, `test_skills.py`, `test_rules.py`, `test_registry.py`,
  `test_engines.py`. Add loader tests for `connection` web + ssh shapes and
  rejection of unknown `type`.

**Acceptance:** existing suite green; `bin/agent-sandbox status` / `ui` behave
identically for opencode/antigravity; a manifest with `connection.type: ssh`
loads and validates.

### MR2 — Generic SSH-remote engine support (host side)

**Goal:** host-side, engine-agnostic plumbing for `connection.type == "ssh"`.

**Files**
- new `packages/agent-sandbox/src/agent_sandbox/ssh.py`:
  - `ensure_ssh_keypair(manifest, sandbox_dir) -> Path`: generate ed25519 keypair
    at `~/.agent-sandbox/<engine>/ssh/id_ed25519` (0600) + `.pub` if missing;
    returns the public key path (for the container mount).
  - `ensure_ssh_client_config(manifest, sandbox_dir, fs_root=None) -> Path`:
    prepend a marker-wrapped literal block into `~/.ssh/config`:
    ```
    # >>> agent-sandbox:<engine> >>>
    Host <host_alias>
        HostName 127.0.0.1
        Port <port>
        User <user>
        IdentityFile ~/.agent-sandbox/<engine>/ssh/id_ed25519
        IdentitiesOnly yes
        StrictHostKeyChecking accept-new
        HostKeyAlias <host_alias>
        UserKnownHostsFile ~/.agent-sandbox/<engine>/ssh/known_hosts
    # <<< agent-sandbox:<engine> <<<
    ```
    Idempotent replace-then-prepend, atomic write (`tempfile.mkstemp` + `os.replace`
    in `~/.ssh/`), `~/.ssh` `0700` / config `0600`. A prepended literal block — not
    an `Include` — because OpenSSH resolves **first-match-wins** (an appended block
    loses to a user's `Host *`) and the Codex desktop app discovers hosts by
    scanning `~/.ssh/config` literally (an `Include`d alias may not appear in its
    picker). `fs_root` is injectable so tests never touch the real `~/.ssh/config`.
  - `remove_ssh_client_config(manifest, sandbox_dir, fs_root=None) -> bool`:
    remove this engine's marker block (enables a future `agent-sandbox detach`).
- `packages/agent-sandbox/src/agent_sandbox/compose/generator.py` — when
  `manifest.connection.type == "ssh"`, mount the generated public half read-only
  into the container at a fixed staging path
  (`/etc/sandbox/ssh/authorized_keys`) so the entrypoint can install it. `web`
  engines unchanged.
- `packages/agent-sandbox/src/agent_sandbox/engines/base.py` — add optional
  `preflight_warnings(self, sandbox_dir: Path) -> tuple[str, ...]` (default `()`).
- `packages/agent-sandbox/src/agent_sandbox/cli.py`:
  - `cmd_ui`: branch on `connection.type` — `web` = today; `ssh` = ensure running,
    `ensure_ssh_client_config`, best-effort `codex features enable remote_connections`
    if `codex` on PATH, `open -a ChatGPT` (fallback `Codex`),
    `open "codex://settings/connections/ssh/add?name=<host_alias>"`, print the one
    manual step ("choose your workspace folder").
  - `cmd_start`/`cmd_status`: print `url` for web, `SSH: <user>@127.0.0.1:<port>
    (alias <host_alias>)` for ssh; call each active adapter's `preflight_warnings`
    and print them (stderr) during `start`.
  - new generic command `shell` (usage `shell <engine> [-- <cmd>…]`) →
    `docker exec -it agent-sandbox-<engine> <cmd…>` (default an interactive shell).
    Used for `codex login --device-auth` and debugging.
- Tests: `ssh.py` unit tests (key perms, prepended block content/order,
  idempotent replace, atomic write + perms, removal),
  generator test for the ssh public-key mount, CLI tests for `ui`/`status`
  branching and `shell` arg construction, adapter-hook default.

**Acceptance:** a synthetic ssh-manifest engine produces a keypair and a
prepended managed `Host` block that wins over a user's `Host *`, the compose
override mounts its public key, `ui` performs the ssh branch, and `shell` builds
the right `docker exec` argv. Web engines unchanged.

### MR3 — `codex` engine definition + adapter

**Goal:** the engine itself.

**Files**
- `engines/codex/manifest.yaml`:
  ```yaml
  name: codex
  port: 58434
  connection:
    type: ssh
    user: developer
    host_alias: agent-sandbox-codex
  rules:
    target_file: ~/.codex/AGENTS.md
    host_sources:
      - ~/.codex/AGENTS.md
  skills:
    target_dir: ~/.agents/skills
  mounts: []
  ```
- `engines/codex/install.sh` (runs as root in the Docker build loop):
  - `apt-get install -y openssh-server netcat-openbsd` (netcat-openbsd required:
    Debian `nc.traditional` lacks `-U`, used by the app-server proxy fallback);
  - `npm install -g @openai/codex@${CODEX_VERSION}`;
  - `ln -sf "$(command -v codex || echo "$NPM_CONFIG_PREFIX/bin/codex")" /usr/local/bin/codex`
    so the **non-interactive login shell** resolves it;
  - write `/etc/ssh/sshd_config.d/codex.conf`:
    ```
    Port 58434
    ListenAddress 0.0.0.0
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
    ```
  - ensure login-shell quiet: guard `/home/developer/.bashrc`/`.profile` against
    non-interactive output (the desktop parses remote stdout; issue #23102).
- `engines/codex/entrypoint.sh`:
  - `mkdir -p ~/.ssh` (0700); install `/etc/sandbox/ssh/authorized_keys` →
    `~/.ssh/authorized_keys` (0600);
  - seed `~/.codex/config.toml` if missing (from `config.toml.example`);
  - `rm -f ~/.codex/app-server-control/*.sock` (stale-socket cleanup);
  - `sudo ssh-keygen -A`; `sudo mkdir -p /run/sshd`;
  - `exec sudo -n /usr/sbin/sshd -D -e`. (developer has `NOPASSWD:ALL`; sshd
    needs root; `StrictModes no` tolerates the volume-mounted home.)
- `engines/codex/config.toml.example`:
  ```toml
  cli_auth_credentials_store = "file"
  sandbox_mode    = "danger-full-access"
  approval_policy = "on-request"
  ```
- `engines/codex/rules/` — optional; empty for now.
- `packages/agent-sandbox/src/agent_sandbox/engines/codex.py`:
  - `CodexUserConfig` (minimal; no fields required for D2) + `parse_user_config`;
  - `scaffold`; `compose_env` (none); `status_lines` (SSH endpoint + auth hint);
  - `preflight_warnings(sandbox_dir)`: compare container `codex --version`
    (`docker exec agent-sandbox-codex codex --version`) with the host app's
    bundled binary (`/Applications/ChatGPT.app/Contents/Resources/codex --version`,
    fallback `/Applications/Codex.app/...`); return a warning naming both versions
    on mismatch (skip silently if either is unavailable).
  - Register in `engines/__init__.py` `_ADAPTERS`.
- `Dockerfile.sandbox`: add `ARG CODEX_VERSION` and pass
  `CODEX_VERSION="${CODEX_VERSION}"` into the existing engine install loop.
- Tests: manifest loads & validates; adapter parse/status/preflight (mock
  subprocess); `test_engines.py` registration.

**Acceptance:** `agent-sandbox build codex` succeeds; `start codex` creates the
container with sshd on 58434; `ssh agent-sandbox-codex 'command -v codex && codex
--version'` works on a non-interactive login shell; `ui codex` configures and
launches the app with the deep link.

### MR4 — Docs

**Files**
- `README.md` — engine table + a "Codex (desktop-remote)" subsection: prerequisites
  (desktop app installed, device-code enabled), `agent-sandbox shell codex` →
  `codex login --device-auth`, the one manual step (pick workspace folder), and
  the E′ fallback note.
- `design/agent_sandbox_architecture.md` — update the authoring guide (manifest
  now uses `connection`, not `web_url`), the file-manifest table (add
  `engines/codex/`), and the operation runbook examples.
- Note: `docs/architecture.svg` refresh deferred.

## 5. Verification

- Unit suite: `python3 -m pytest packages/agent-sandbox/tests` (or
  `python3 -m unittest discover -s packages/agent-sandbox/tests`) with
  `PYTHONPATH=packages/agent-sandbox/src`.
- Container-level (runnable in this Linux env): build the image; start `codex`;
  assert sshd listens on the engine port; `ssh` in and assert `codex --version`
  resolves on a **non-interactive login shell** with no motd/login noise; run a
  `codex app-server` `initialize` handshake.
- The macOS desktop app cannot be exercised here; we validate the container
  endpoint, generated SSH config, and version-warning logic.

## 6. Risks

- sshd must run as root in a container whose main user is non-root → entrypoint
  uses `sudo -n` (already granted in `Dockerfile.sandbox`).
- Docker Desktop/OrbStack UDS limitation → `CODEX_HOME` stays on `agent_home_codex`.
- Version skew is only partly surfaced by Codex → our `preflight_warnings` covers it.
- Codex desktop internals are closed-source; the SSH flow is documented but may
  evolve (the deep link `…/ssh/add?name=` is documented today).

## 7. Deferred / future

- E′ fallback (`CODEX_APP_SERVER_WS_URL=ws://127.0.0.1:<port>`) — document only.
- Automatic desktop-app version matching (build-time auto-detect of the app's
  bundled `codex --version`).
- Seeding `~/.codex/.codex-global-state.json` / `remote-projects` (unsupported, races).
- API-key auth.
