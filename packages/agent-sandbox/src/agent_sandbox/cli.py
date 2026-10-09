"""Command-line interface orchestrator for agent sandbox."""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config.loader import load_sandbox_config, save_sandbox_config
from .config.models import EngineManifest, SandboxConfig
from .compose.generator import generate_compose_override
from .compose.rules import compile_rules_for_engine
from .engines import CONTAINER_NAME_PREFIX, all_adapters, get_runtime, load_all_runtimes, resolve_engine_name, runtimes_for_active, scaffold_all
from .framework import CLIApp, argument, generate_completion_script, option
from .registry.engines import discover_engines
from .skills.catalog import CatalogSkillResolver
from .skills.directory import DirectorySkillResolver


def get_repo_root() -> str:
    """Returns the repository root path from AGENT_SANDBOX_REPO_ROOT or fails fast."""
    repo_root = os.environ.get("AGENT_SANDBOX_REPO_ROOT")
    if not repo_root:
        candidate = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
        if os.path.isdir(os.path.join(candidate, "engines")):
            return candidate
        raise RuntimeError(
            "AGENT_SANDBOX_REPO_ROOT environment variable is not set. "
            "Please invoke agent-sandbox via bin/agent-sandbox or export AGENT_SANDBOX_REPO_ROOT."
        )
    repo_root = os.path.abspath(os.path.expanduser(repo_root))
    if not os.path.isdir(repo_root):
        raise RuntimeError(
            f"AGENT_SANDBOX_REPO_ROOT directory does not exist: {repo_root}"
        )
    return repo_root


def resolve_engines(cli: SandboxCLI) -> list[str]:
    try:
        return [e.name for e in cli.registry.list_all()]
    except Exception:
        return []


def resolve_whitelisted_workspaces(cli: SandboxCLI) -> list[str]:
    try:
        config = cli.ensure_scaffolding()
        return list(config.allowed_workspaces)
    except Exception:
        return []


def resolve_sandbox_entries(cli: SandboxCLI) -> list[str]:
    try:
        if os.path.isdir(cli.sandbox_dir):
            return [f for f in os.listdir(cli.sandbox_dir) if not f.startswith(".")]
    except Exception:
        pass
    return []


app = CLIApp(name="agent-sandbox")
ws_group = app.group(
    name="workspace",
    aliases=["ws"],
    help="Whitelist workspace directory",
    default_command="list",
)
hb_group = app.group(
    name="host-bridge",
    help="Manage the macOS host execution bridge",
    default_command="status",
    custom_usage="host-bridge [start|stop|restart|status|fg]",
)
completion_group = app.group(
    name="completion",
    help="Generate or install shell tab-completion scripts (zsh, bash)",
    default_command="zsh",
    custom_usage="completion [zsh|bash|install]",
)


class SandboxCLI:
    def __init__(
        self,
        repo_root: str | None = None,
        sandbox_dir: str = "~/.agent-sandbox",
        fs_root: Path = Path.home(),
    ) -> None:
        self.fs_root = fs_root
        self.repo_root = repo_root or get_repo_root()
        self.sandbox_dir = os.path.abspath(os.path.expanduser(sandbox_dir))
        self.config_file = os.path.join(self.sandbox_dir, "whitelist.yaml")

        self.override_file = os.path.join(self.sandbox_dir, "docker-compose.override.yml")
        self.ipc_dir = os.path.join(self.sandbox_dir, "ipc")
        self.logs_dir = os.path.join(self.sandbox_dir, "logs")
        self.host_bridge_pid_file = os.path.join(self.ipc_dir, "host-bridge.pid")
        self.host_bridge_log_file = os.path.join(self.logs_dir, "host_exec_daemon.log")

        self.engines_dir = os.path.join(self.repo_root, "engines")
        self.registry = discover_engines(self.engines_dir)

    def ensure_scaffolding(self) -> SandboxConfig:
        """Ensures container-first directories, IPC, logs, and default config exist."""
        self.migrate_legacy_state()
        scaffold_dirs = [
            self.sandbox_dir,
            self.ipc_dir,
            self.logs_dir,
            os.path.join(self.sandbox_dir, "common", "rules"),
            os.path.join(self.sandbox_dir, "common", "skills"),
        ]

        for engine in self.registry.list_all():
            scaffold_dirs.append(os.path.join(self.sandbox_dir, engine.name, "rules"))
            scaffold_dirs.append(os.path.join(self.sandbox_dir, engine.name, "skills"))

        for d in scaffold_dirs:
            os.makedirs(d, exist_ok=True)

        scaffold_all(Path(self.sandbox_dir), Path(self.repo_root))

        try:
            os.chmod(self.ipc_dir, 0o700)
        except Exception:
            pass

        config: SandboxConfig | None = None
        if os.path.isfile(self.config_file):
            config = load_sandbox_config(self.config_file)

        if config is None:
            if not os.path.isfile(self.config_file):
                default_template = os.path.join(self.repo_root, "config", "whitelist.default.yaml")
                if os.path.isfile(default_template):
                    try:
                        shutil.copyfile(default_template, self.config_file)
                    except Exception as e:
                        print(f"[Sandbox Warning] Failed to copy default config: {e}", file=sys.stderr)
            config = load_sandbox_config(self.config_file)

        return config

    def load_engine_runtimes(self):
        """Parses every engine's user config exactly once (typed, fail fast)."""
        self.ensure_scaffolding()
        return load_all_runtimes(self.registry.list_all(), self.sandbox_dir)

    def run_legacy_engine_migrations(self) -> None:
        """Runs engine-owned legacy migrations via generic adapter hook."""
        for adapter in all_adapters():
            adapter.run_legacy_migrations()

    def migrate_legacy_state(self, legacy_dir: str | None = None) -> None:
        """Copy legacy state once, before the canonical state directory is created."""
        legacy_dir = legacy_dir or os.path.expanduser("~/.antigravity-sandbox")
        if os.path.exists(self.sandbox_dir) or not os.path.isdir(legacy_dir):
            return

        parent_dir = os.path.dirname(self.sandbox_dir)
        os.makedirs(parent_dir, exist_ok=True)
        staging_dir = f"{self.sandbox_dir}.migration-tmp"
        if os.path.exists(staging_dir):
            raise RuntimeError(f"Legacy migration staging directory already exists: {staging_dir}")

        try:
            shutil.copytree(legacy_dir, staging_dir)
            os.replace(staging_dir, self.sandbox_dir)
        except Exception as exc:
            if os.path.exists(staging_dir):
                shutil.rmtree(staging_dir, ignore_errors=True)
            raise RuntimeError(f"Failed to migrate legacy state from {legacy_dir}: {exc}") from exc

        print(
            f"[Sandbox Migration] Copied legacy state {legacy_dir} -> {self.sandbox_dir}",
            file=sys.stderr,
        )

    def run_compose(self, compose_args: list[str]) -> int:
        """Executes docker compose against the base project plus the generated override."""

        cmd = ["docker", "compose", "-f", os.path.join(self.repo_root, "docker-compose.yml")]
        if os.path.isfile(self.override_file):
            cmd.extend(["-f", self.override_file])

        cmd.extend(compose_args)

        env = os.environ.copy()
        env["HOST_UID"] = str(os.getuid() if hasattr(os, "getuid") else 1000)
        env["HOST_GID"] = str(os.getgid() if hasattr(os, "getgid") else 1000)
        env["HOST_IPC_PATH"] = self.ipc_dir

        res = subprocess.run(cmd, env=env, check=False)
        return res.returncode

    def get_host_bridge_pid(self) -> int | None:
        """Retrieves active PID of host-exec daemon if running."""
        if os.path.isfile(self.host_bridge_pid_file):
            try:
                with open(self.host_bridge_pid_file, "r", encoding="utf-8") as f:
                    pid = int(f.read().strip())
                # Check process existence
                os.kill(pid, 0)
                return pid
            except (ValueError, OSError):
                pass
        return None

    def is_host_bridge_port_open(self, host: str = "127.0.0.1", port: int = 58433) -> bool:
        """Checks if a process is already listening on the host bridge port."""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect((host, port))
                return True
        except (OSError, ConnectionRefusedError):
            return False

    def start_host_bridge(self) -> None:
        """Starts host-exec daemon in background if not running."""
        pid = self.get_host_bridge_pid()
        if pid:
            print(f"[Bridge] Host-Exec daemon is already running (PID: {pid}).")
            return
        if self.is_host_bridge_port_open():
            print("[Bridge] Host-Exec daemon port 58433 is already in use.")
            return

        daemon_script = os.path.join(
            self.repo_root, "packages", "host-exec-daemon", "host_exec_daemon.py"
        )
        if not os.path.isfile(daemon_script):
            print(f"[Bridge Warning] Daemon script not found at {daemon_script}", file=sys.stderr)
            return

        print("[Bridge] Spawning Host-Exec daemon in background...")
        os.makedirs(self.logs_dir, exist_ok=True)
        os.makedirs(self.ipc_dir, exist_ok=True)

        with open(self.host_bridge_log_file, "a", encoding="utf-8") as out_f:
            env = os.environ.copy()
            env["AGENT_SANDBOX_STATE_DIR"] = self.sandbox_dir
            env["HOST_EXEC_PORT"] = "58433"
            proc = subprocess.Popen(
                [sys.executable, daemon_script],
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=out_f,
                stderr=out_f,
                start_new_session=True,
            )

        with open(self.host_bridge_pid_file, "w", encoding="utf-8") as pf:
            pf.write(str(proc.pid))

        # Wait briefly to confirm
        time.sleep(0.3)
        if proc.poll() is None:
            print(f"[Bridge] Host-Exec daemon started in background (PID: {proc.pid}, listening on port 58433).")
        else:
            print(f"[Bridge Warning] Host-Exec daemon exited immediately. Check: {self.host_bridge_log_file}", file=sys.stderr)

    def stop_host_bridge(self) -> None:
        """Stops background host-exec daemon."""
        pid = self.get_host_bridge_pid()
        if pid:
            print(f"[Bridge] Stopping Host-Exec daemon (PID: {pid})...")
            try:
                os.kill(pid, 15)  # SIGTERM
                for _ in range(30):
                    time.sleep(0.1)
                    os.kill(pid, 0)
            except OSError:
                pass
            try:
                os.kill(pid, 9)  # SIGKILL fallback
            except OSError:
                pass
            if os.path.isfile(self.host_bridge_pid_file):
                try:
                    os.remove(self.host_bridge_pid_file)
                except OSError:
                    pass
            print("[Bridge] Host-Exec daemon stopped.")
        else:
            if os.path.isfile(self.host_bridge_pid_file):
                try:
                    os.remove(self.host_bridge_pid_file)
                except OSError:
                    pass
            if self.is_host_bridge_port_open():
                print("[Bridge Warning] Host-bridge PID file was missing, but port 58433 is still listening.", file=sys.stderr)
                print("[Bridge Warning] You may need to terminate the process listening on port 58433 manually (e.g. 'lsof -ti :58433 | xargs kill').", file=sys.stderr)

    def get_running_engines(self) -> tuple[EngineManifest, ...]:
        """Queries Docker for currently running agent sandbox containers.

        Returns resolved manifests. A container wearing our prefix that maps to
        no registered engine raises KeyError (unexpected tooling/reality drift).
        Docker being unreachable degrades to empty, as before.
        """
        try:
            res = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception:
            return ()
        if res.returncode != 0:
            return ()
        names = [
            name
            for line in res.stdout.splitlines()
            if (name := resolve_engine_name(line)) is not None
        ]
        return tuple(self.registry.resolve_active(names))

    # Command Handlers
    @app.command(
        "start",
        help="Start one or more engines (e.g. opencode antigravity)",
        custom_usage="start <engine...> [--no-host-bridge]",
    )
    @argument("engines", nargs="*", required=False, help="Engines to start", complete=resolve_engines)
    @option("--no-host-bridge", is_flag=True, dest="no_host_bridge", value=True, help="Do not start host bridge")
    @option("--with-host-bridge", is_flag=True, dest="no_host_bridge", value=False, help="Start host bridge")
    def cmd_start(self, engines: list[str], no_host_bridge: bool = False) -> int:
        if not engines:
            available = ", ".join(e.name for e in self.registry.list_all())
            print(f"[Sandbox Error] No engine specified. Please specify which engine to start. Available engines: {available}", file=sys.stderr)
            return 1

        config = self.ensure_scaffolding()
        self.run_legacy_engine_migrations()

        # Parse-once: engine user configs loaded a single time for this command.
        all_runtimes = load_all_runtimes(self.registry.list_all(), self.sandbox_dir)

        active = self.registry.resolve_active(engines)
        if not active:
            print("[Sandbox Error] No valid engines resolved to start.", file=sys.stderr)
            return 1

        active_runtimes = runtimes_for_active(active, all_runtimes)
        active_names = [e.name for e in active]
        print(f"[Sandbox] Preparing engines: {', '.join(active_names)}")

        # 1. Compile rules
        for e in active:
            compile_rules_for_engine(e, repo_root=self.repo_root, sandbox_dir=self.sandbox_dir)

        # 2. Generate override
        generate_compose_override(
            active_engines=active,
            config=config,
            repo_root=self.repo_root,
            sandbox_dir=self.sandbox_dir,
            override_file=self.override_file,
            fs_root=self.fs_root,
            engine_runtimes=active_runtimes,
        )

        # 3. Host bridge
        if not no_host_bridge:
            self.start_host_bridge()

        # 4. Launch containers
        print(f"[Sandbox] Starting containers with VirtioFS (engines: {', '.join(active_names)})...")
        code = self.run_compose(["up", "-d"] + active_names)
        if code == 0:
            print("[Sandbox] Containers started successfully:")
            for e in active:
                print(f"  - [{e.name.upper()}] Web UI: {e.web_url} (Port {e.port})")
        return code

    def _ensure_services_in_override(self, engines: Sequence[EngineManifest]) -> None:
        """Ensures the specified engines are defined in docker-compose.override.yml."""
        common_cfg = os.path.join(self.repo_root, "config", "compose.common.yaml")
        if not os.path.isfile(common_cfg):
            return

        needs_generation = False
        if not os.path.isfile(self.override_file):
            needs_generation = True
        else:
            try:
                import yaml
                with open(self.override_file, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                services = data.get("services", {}) if isinstance(data, dict) else {}
                for e in engines:
                    if e.name not in services:
                        needs_generation = True
                        break
            except Exception:
                needs_generation = True

        if needs_generation:
            try:
                config = self.ensure_scaffolding()
                all_runtimes = load_all_runtimes(self.registry.list_all(), self.sandbox_dir)
                active_runtimes = runtimes_for_active(engines, all_runtimes)
                for e in engines:
                    compile_rules_for_engine(e, repo_root=self.repo_root, sandbox_dir=self.sandbox_dir)
                generate_compose_override(
                    active_engines=engines,
                    config=config,
                    repo_root=self.repo_root,
                    sandbox_dir=self.sandbox_dir,
                    override_file=self.override_file,
                    fs_root=self.fs_root,
                    engine_runtimes=active_runtimes,
                )
            except Exception as exc:
                print(f"[Sandbox Warning] Could not regenerate compose override: {exc}", file=sys.stderr)

    @app.command(
        "stop",
        help="Stop one or more engines (bridge stops when none remain)",
        custom_usage="stop [engine...]",
    )
    @argument("engines", nargs="*", required=False, help="Engines to stop", complete=resolve_engines)
    def cmd_stop(self, engines: list[str] | None = None) -> int:
        if engines:
            active = self.registry.resolve_active(engines)
            if not active:
                print("[Sandbox Error] No valid engines resolved to stop.", file=sys.stderr)
                return 1
            active_names = [e.name for e in active]
            remaining = {m.name for m in self.get_running_engines()} - set(active_names)
            print(f"[Sandbox] Stopping engines: {', '.join(active_names)}...")
            self._ensure_services_in_override(active)
            code = self.run_compose(["stop"] + active_names)
            if code != 0:
                # Fallback: stop active engine containers directly via docker stop
                fallback_success = True
                for name in active_names:
                    candidates = [f"{CONTAINER_NAME_PREFIX}{name}"]
                    adapter = next((a for a in all_adapters() if a.name == name), None)
                    if adapter:
                        candidates.extend(adapter.legacy_container_names())
                    stopped = False
                    for cname in candidates:
                        try:
                            res = subprocess.run(
                                ["docker", "stop", cname],
                                capture_output=True,
                                text=True,
                                check=False,
                            )
                            if res.returncode == 0:
                                stopped = True
                                print(f"[Sandbox] Stopped container '{cname}' directly via Docker.")
                                break
                        except Exception:
                            pass
                    if not stopped:
                        fallback_success = False
                if fallback_success:
                    code = 0

            if code == 0 and not remaining:
                self.stop_host_bridge()
            return code
        code = self.run_compose(["down"])
        if code != 0:
            for running_engine in self.get_running_engines():
                cname = f"{CONTAINER_NAME_PREFIX}{running_engine.name}"
                try:
                    subprocess.run(["docker", "stop", cname], capture_output=True, check=False)
                except Exception:
                    pass
        self.stop_host_bridge()
        return code

    @app.command(
        "restart",
        help="Restart one or more engines (others left running)",
        custom_usage="restart <engine...>",
    )
    @argument("engines", nargs="*", required=False, help="Engines to restart", complete=resolve_engines)
    @option("--no-host-bridge", is_flag=True, dest="no_host_bridge", value=True, help="Do not start host bridge")
    @option("--with-host-bridge", is_flag=True, dest="no_host_bridge", value=False, help="Start host bridge")
    def cmd_restart(self, engines: list[str], no_host_bridge: bool = False) -> int:
        if not engines:
            available = ", ".join(e.name for e in self.registry.list_all())
            print(f"[Sandbox Error] No engine specified. Please specify which engine to restart. Available engines: {available}", file=sys.stderr)
            return 1
        self.cmd_stop(engines)
        return self.cmd_start(engines, no_host_bridge=no_host_bridge)

    @app.command(
        "build",
        help="Rebuild the sandbox container image",
        custom_usage="build [engine...]",
    )
    @argument("engines", nargs="*", required=False, help="Engines to build", complete=resolve_engines)
    def cmd_build(self, engines: list[str] | None = None) -> int:
        config = self.ensure_scaffolding()
        active = self.registry.resolve_active(engines) if engines else self.registry.list_all()
        all_runtimes = load_all_runtimes(self.registry.list_all(), self.sandbox_dir)
        active_runtimes = runtimes_for_active(active, all_runtimes)
        generate_compose_override(
            active_engines=active,
            config=config,
            repo_root=self.repo_root,
            sandbox_dir=self.sandbox_dir,
            override_file=self.override_file,
            fs_root=self.fs_root,
            engine_runtimes=active_runtimes,
        )
        print("[Sandbox] Building/Rebuilding container image from Dockerfile.sandbox...")
        return self.run_compose(["build"] + [e.name for e in active])

    @app.command("status", help="Display sandbox, engine, and bridge status")
    def cmd_status(self) -> int:
        config = self.ensure_scaffolding()
        all_runtimes = load_all_runtimes(self.registry.list_all(), self.sandbox_dir)
        print("==========================================================")
        print("  Agent Sandbox Status")
        print("==========================================================")
        running = self.get_running_engines()
        running_names = {m.name for m in running}
        print(f"Available Engines ({len(all_runtimes)}):")
        for runtime in all_runtimes:
            manifest = runtime.manifest
            state = "RUNNING" if manifest.name in running_names else "STOPPED"
            print(f"  - {manifest.name:<14} [{state:<7}] URL: {manifest.web_url}")
            for line in runtime.adapter.status_lines(runtime.config):
                print(f"      {line}")

        hb_pid = self.get_host_bridge_pid()
        if hb_pid:
            hb_state = f"Active (PID: {hb_pid}, port 58433)"
        elif self.is_host_bridge_port_open():
            hb_state = "Active (port 58433 listening, PID unknown)"
        else:
            hb_state = "Inactive"
        print(f"\nHost-Exec Bridge: {hb_state}")

        print(f"\nWhitelisted Workspaces ({len(config.allowed_workspaces)}):")
        if not config.allowed_workspaces:
            print("  (None whitelisted yet. Use: agent-sandbox workspace add <path>)")
        for ws in config.allowed_workspaces:
            status = "EXISTS" if os.path.isdir(os.path.expanduser(ws)) else "NOT FOUND"
            print(f"  - {ws} [{status}]")
        print("==========================================================")
        return 0

    @app.command(
        "ui",
        aliases=["web"],
        help="Open engine Web UI in browser (defaults to active)",
        custom_usage="ui [engine]",
    )
    @argument("engine_name", nargs="?", required=False, default=None, help="Engine name", complete=resolve_engines)
    def cmd_ui(self, engine_name: str | None = None) -> int:
        self.ensure_scaffolding()
        all_runtimes = load_all_runtimes(self.registry.list_all(), self.sandbox_dir)
        running = self.get_running_engines()

        target_engine: EngineManifest | None = None
        if engine_name:
            target_engine = self.registry.get(engine_name)
        elif len(running) == 1:
            target_engine = running[0]
        elif len(running) > 1:
            names = ", ".join(e.name for e in running)
            print(f"[Sandbox Error] Multiple engines are running ({names}). Please specify which engine UI to open (e.g. agent-sandbox ui {running[0].name}).", file=sys.stderr)
            return 1
        else:
            available = ", ".join(e.name for e in self.registry.list_all())
            print(f"[Sandbox Error] No engines currently running. Please specify which engine to open/start. Available engines: {available}", file=sys.stderr)
            return 1

        if not target_engine:
            print("[Sandbox Error] No engine found to open UI.", file=sys.stderr)
            return 1

        if target_engine not in running:
            print(f"[Sandbox] Engine '{target_engine.name}' is not running. Starting...")
            self.cmd_start([target_engine.name])

        url = target_engine.web_url
        print(f"[Sandbox] Opening {target_engine.name} Web UI ({url})...")
        runtime = get_runtime(all_runtimes, target_engine.name)
        for line in runtime.adapter.status_lines(runtime.config):
            print(f"[Sandbox] {target_engine.name} {line}")
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        else:
            if shutil.which("xdg-open"):
                subprocess.run(["xdg-open", url], check=False)
            else:
                print(f"[Sandbox] Please open {url} in your browser.")
        return 0

    @ws_group.command(
        "add",
        help="Whitelist workspace directory",
        custom_usage="workspace add [path] [-y|--force]",
    )
    @argument("path", nargs="?", required=False, default=None, help="Workspace directory path", complete="directories")
    @option("-y", "--force", is_flag=True, dest="force", value=True, help="Create directory if missing")
    def cmd_workspace_add(self, path: str | None = None, force: bool = False) -> int:
        config = self.ensure_scaffolding()
        target = os.path.abspath(os.path.expanduser(path or os.getcwd()))

        if not os.path.isdir(target):
            if force:
                os.makedirs(target, exist_ok=True)
            else:
                print(f"[Sandbox Error] Directory does not exist: {target}", file=sys.stderr)
                return 1

        curr_workspaces = list(config.allowed_workspaces)
        if target in curr_workspaces:
            print(f"[Sandbox] Workspace already whitelisted: {target}")
            return 0

        curr_workspaces.append(target)
        new_config = SandboxConfig(
            allowed_workspaces=tuple(curr_workspaces),
            allowed_commands=config.allowed_commands,
        )
        save_sandbox_config(new_config, self.config_file)
        print(f"[Sandbox] Successfully whitelisted workspace: {target}")

        running = self.get_running_engines()
        if running:
            print("[Sandbox] Updating running containers with new workspace mount...")
            self.cmd_start([e.name for e in running])
        return 0

    @ws_group.command(
        "remove",
        aliases=["rm"],
        help="Remove workspace from whitelist",
        custom_usage="workspace remove <path>",
    )
    @argument("path", required=True, help="Workspace directory to remove", complete=resolve_whitelisted_workspaces)
    def cmd_workspace_remove(self, path: str) -> int:
        config = self.ensure_scaffolding()
        target = os.path.abspath(os.path.expanduser(path))
        curr_workspaces = list(config.allowed_workspaces)
        if target not in curr_workspaces:
            print(f"[Sandbox] Workspace not in whitelist: {target}")
            return 0

        curr_workspaces.remove(target)
        new_config = SandboxConfig(
            allowed_workspaces=tuple(curr_workspaces),
            allowed_commands=config.allowed_commands,
        )
        save_sandbox_config(new_config, self.config_file)
        print(f"[Sandbox] Removed workspace: {target}")

        running = self.get_running_engines()
        if running:
            print("[Sandbox] Updating running containers...")
            self.cmd_start([e.name for e in running])
        return 0

    @ws_group.command(
        "list",
        aliases=["ls"],
        help="List whitelisted workspaces",
        custom_usage="workspace list",
    )
    def cmd_workspace_list(self) -> int:
        config = self.ensure_scaffolding()
        print(f"Whitelisted Workspaces ({len(config.allowed_workspaces)}):")
        for ws in config.allowed_workspaces:
            print(f"  - {ws}")
        return 0

    @app.command(
        "rules",
        help="Display compiled rules and sources",
        custom_usage="rules [engine]",
    )
    @argument("engine_name", nargs="?", required=False, default=None, help="Engine name", complete=resolve_engines)
    def cmd_rules(self, engine_name: str | None = None) -> int:
        config = self.ensure_scaffolding()
        engines = [self.registry.get(engine_name)] if engine_name else self.registry.list_all()

        print("==========================================================")
        print("  Compiled Agent Sandbox Rules")
        print("==========================================================")
        for e in engines:
            if not e.rules or not e.rules.target_file:
                continue
            res = compile_rules_for_engine(e, repo_root=self.repo_root, sandbox_dir=self.sandbox_dir)
            print(f"\nEngine: {e.name.upper()}")
            print(f"Target Container File: {res.target_container_file}")
            print(f"Host Compiled File   : {res.host_compiled_file}")
            print(f"Rule Sources ({len(res.sources)}):")
            for s in res.sources:
                print(f"  [{s.category}] {s.description} ({s.source_path})")
        print("==========================================================")
        return 0

    @app.command(
        "skills",
        help="Display discovered skills",
        custom_usage="skills [engine]",
    )
    @argument("engine_name", nargs="?", required=False, default=None, help="Engine name", complete=resolve_engines)
    def cmd_skills(self, engine_name: str | None = None) -> int:
        config = self.ensure_scaffolding()
        engines = [self.registry.get(engine_name)] if engine_name else self.registry.list_all()
        dir_resolver = DirectorySkillResolver(self.sandbox_dir)
        catalog_resolver = CatalogSkillResolver(config.allowed_workspaces)

        print("==========================================================")
        print("  Discovered Skills")
        print("==========================================================")
        for e in engines:
            print(f"\nEngine: {e.name.upper()}")
            dir_skills = dir_resolver.resolve_skills_for_engine(e.name)
            print(f"  Directory Skills ({len(dir_skills)}):")
            for name, path in sorted(dir_skills.items()):
                print(f"    - {name}: {path}")

            cat_skills = catalog_resolver.resolve_for_engine(e, warn=False)
            print(f"  Catalog Skills ({len(cat_skills)}):")
            for cp in sorted(cat_skills):
                print(f"    - {cp}")
        print("==========================================================")
        return 0

    @app.command(
        "open",
        help="Open sandbox state folder in desktop file manager",
        custom_usage="open [subpath]",
    )
    @argument("target", nargs="?", required=False, default=None, help="Subpath to open", complete=resolve_sandbox_entries)
    def cmd_open(self, target: str | None = None) -> int:
        dest = self.sandbox_dir
        if target:
            candidate = os.path.join(self.sandbox_dir, target)
            if os.path.exists(candidate):
                dest = candidate
            elif os.path.exists(target):
                dest = os.path.abspath(target)

        print(f"[Sandbox] Opening: {dest}")
        if sys.platform == "darwin":
            subprocess.run(["open", dest], check=False)
        else:
            if shutil.which("xdg-open"):
                subprocess.run(["xdg-open", dest], check=False)
        return 0

    @hb_group.command("start", aliases=["--bg", "-d"], help="Start Host-Exec daemon in background")
    def cmd_host_bridge_start(self) -> int:
        self.start_host_bridge()
        return 0

    @hb_group.command("stop", help="Stop Host-Exec daemon")
    def cmd_host_bridge_stop(self) -> int:
        self.stop_host_bridge()
        return 0

    @hb_group.command("restart", help="Restart Host-Exec daemon")
    def cmd_host_bridge_restart(self) -> int:
        self.stop_host_bridge()
        self.start_host_bridge()
        return 0

    @hb_group.command("status", help="Check Host-Exec daemon status")
    def cmd_host_bridge_status(self) -> int:
        pid = self.get_host_bridge_pid()
        if pid:
            print(f"[Status] Host-Exec daemon: Active (PID: {pid}, Listening on port 58433)")
        elif self.is_host_bridge_port_open():
            print("[Status] Host-Exec daemon: Active (Listening on port 58433, PID unknown)")
        else:
            print("[Status] Host-Exec daemon: Inactive")
        return 0

    @hb_group.command("fg", aliases=["run"], help="Run Host-Exec daemon in foreground")
    def cmd_host_bridge_fg(self) -> int:
        pid = self.get_host_bridge_pid()
        if pid:
            print(f"[Bridge Error] Host-Exec daemon is already running in background (PID: {pid}).", file=sys.stderr)
            print("Use 'agent-sandbox host-bridge stop' to stop it before running in foreground.", file=sys.stderr)
            return 1
        if self.is_host_bridge_port_open():
            print("[Bridge Error] Host-Exec daemon port 58433 is already in use by another process.", file=sys.stderr)
            return 1
        daemon_script = os.path.join(self.repo_root, "packages", "host-exec-daemon", "host_exec_daemon.py")
        env = os.environ.copy()
        env["AGENT_SANDBOX_STATE_DIR"] = self.sandbox_dir
        env["HOST_EXEC_PORT"] = "58433"
        return subprocess.run([sys.executable, daemon_script], env=env).returncode

    @completion_group.command("zsh", help="Output Zsh tab-completion script")
    def cmd_completion_zsh(self) -> int:
        print(generate_completion_script("zsh", "agent-sandbox"))
        return 0

    @completion_group.command("bash", help="Output Bash tab-completion script")
    def cmd_completion_bash(self) -> int:
        print(generate_completion_script("bash", "agent-sandbox"))
        return 0

    @completion_group.command("install", help="Install tab-completion loader into shell profile (~/.zshrc or ~/.bashrc)")
    @option("--shell", dest="shell", is_flag=False, default=None, help="Target shell (zsh or bash)")
    def cmd_completion_install(self, shell: str | None = None) -> int:
        target_shell = shell
        if not target_shell:
            user_shell = os.environ.get("SHELL", "").lower()
            if "bash" in user_shell:
                target_shell = "bash"
            else:
                target_shell = "zsh"

        if target_shell not in ("zsh", "bash"):
            print(f"[Completion Error] Unsupported shell: {target_shell}. Supported: zsh, bash", file=sys.stderr)
            return 1

        rc_file = os.path.expanduser("~/.bashrc" if target_shell == "bash" else "~/.zshrc")
        eval_line = f'eval "$(agent-sandbox completion {target_shell})"'

        if os.path.isfile(rc_file):
            try:
                with open(rc_file, "r", encoding="utf-8") as f:
                    content = f.read()
                if eval_line in content:
                    print(f"[Completion] Tab-completion is already configured in {rc_file}.")
                    return 0
            except Exception:
                pass

        try:
            with open(rc_file, "a", encoding="utf-8") as f:
                f.write(f"\n# Enable agent-sandbox tab-completion\n{eval_line}\n")
            print(f"[Completion] Successfully added tab-completion loader to {rc_file}.")
            print(f"[Completion] Run 'source {rc_file}' or start a new terminal session to enable it.")
            return 0
        except Exception as exc:
            print(f"[Completion Error] Failed to write to {rc_file}: {exc}", file=sys.stderr)
            return 1


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]

    # Fast-path for tab completion queries
    if args and args[0] == "_complete":
        if len(args) < 4:
            return 0
        shell = args[1]
        try:
            cword = int(args[2])
        except ValueError:
            return 0
        words = args[3:]
        cli = SandboxCLI()
        for candidate in app.complete(cli, shell, cword, words):
            print(candidate)
        return 0

    cli = SandboxCLI()
    return app.dispatch(cli, args)


if __name__ == "__main__":
    sys.exit(main())
