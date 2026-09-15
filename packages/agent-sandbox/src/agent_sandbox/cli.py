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
from .compose.volumes import migrate_antigravity_volume_if_needed
from .registry.engines import discover_engines
from .skills.catalog import CatalogSkillResolver
from .skills.directory import DirectorySkillResolver


def get_repo_root() -> str:
    """Returns the repository root path from AGENT_SANDBOX_REPO_ROOT or fails fast."""
    repo_root = os.environ.get("AGENT_SANDBOX_REPO_ROOT")
    if not repo_root:
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
        """Executes docker compose against the base project plus the generated override.

        The override file defines exactly the requested engine services, so no profiles
        are required; `docker compose up` operates solely on the engines present in it.
        """
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

    def start_host_bridge(self) -> None:
        """Starts host-exec daemon in background if not running."""
        pid = self.get_host_bridge_pid()
        if pid:
            print(f"[Bridge] Host-Exec daemon is already running (PID: {pid}).")
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

    def get_running_engines(self) -> list[str]:
        """Queries Docker for currently running agent sandbox containers."""
        try:
            res = subprocess.run(
                ["docker", "ps", "--format", "{{.Names}}"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0:
                names = res.stdout.splitlines()
                running = []
                for n in names:
                    n = n.strip()
                    if n.startswith("agent-sandbox-"):
                        running.append(n[len("agent-sandbox-"):])
                    elif n == "antigravity-sandbox":
                        running.append("antigravity")
                return running
        except Exception:
            pass
        return []

    # Command Handlers
    def cmd_start(self, engines: list[str], no_host_bridge: bool = False) -> int:
        if not engines:
            available = ", ".join(e.name for e in self.registry.list_all())
            print(f"[Sandbox Error] No engine specified. Please specify which engine to start. Available engines: {available}", file=sys.stderr)
            return 1

        config = self.ensure_scaffolding()
        migrate_antigravity_volume_if_needed()

        active = self.registry.resolve_active(engines)
        if not active:
            print("[Sandbox Error] No valid engines resolved to start.", file=sys.stderr)
            return 1

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
        )

        # 3. Host bridge
        if not no_host_bridge:
            self.start_host_bridge()

        # 4. Launch containers
        print(f"[Sandbox] Starting containers with VirtioFS (engines: {', '.join(active_names)})...")
        code = self.run_compose(["up", "-d"])
        if code == 0:
            print("[Sandbox] Containers started successfully:")
            for e in active:
                print(f"  - [{e.name.upper()}] Web UI: {e.web_url} (Port {e.port})")
        return code

    def cmd_stop(self, engines: list[str] | None = None) -> int:
        if engines:
            self.registry.resolve_active(engines)
            code = self.run_compose(["stop"])
        else:
            code = self.run_compose(["down"])
            self.stop_host_bridge()
        return code

    def cmd_restart(self, engines: list[str], no_host_bridge: bool = False) -> int:
        self.cmd_stop(engines)
        return self.cmd_start(engines, no_host_bridge=no_host_bridge)

    def cmd_build(self, engines: list[str] | None = None) -> int:
        config = self.ensure_scaffolding()
        active = self.registry.resolve_active(engines) if engines else self.registry.list_all()
        generate_compose_override(
            active_engines=active,
            config=config,
            repo_root=self.repo_root,
            sandbox_dir=self.sandbox_dir,
            override_file=self.override_file,
            fs_root=self.fs_root,
        )
        print("[Sandbox] Building/Rebuilding container image from Dockerfile.sandbox...")
        return self.run_compose(["build"])

    def cmd_status(self) -> int:
        config = self.ensure_scaffolding()
        print("==========================================================")
        print("  Agent Sandbox Status")
        print("==========================================================")
        running = self.get_running_engines()
        all_engines = self.registry.list_all()
        print(f"Available Engines ({len(all_engines)}):")
        for e in all_engines:
            state = "RUNNING" if e.name in running else "STOPPED"
            print(f"  - {e.name:<14} [{state:<7}] URL: {e.web_url}")

        hb_pid = self.get_host_bridge_pid()
        hb_state = f"Active (PID: {hb_pid}, port 58433)" if hb_pid else "Inactive"
        print(f"\nHost-Exec Bridge: {hb_state}")

        print(f"\nWhitelisted Workspaces ({len(config.allowed_workspaces)}):")
        if not config.allowed_workspaces:
            print("  (None whitelisted yet. Use: agent-sandbox workspace add <path>)")
        for ws in config.allowed_workspaces:
            status = "EXISTS" if os.path.isdir(os.path.expanduser(ws)) else "NOT FOUND"
            print(f"  - {ws} [{status}]")
        print("==========================================================")
        return 0

    def cmd_ui(self, engine_name: str | None = None) -> int:
        config = self.ensure_scaffolding()
        running = self.get_running_engines()

        target_engine: EngineManifest | None = None
        if engine_name:
            target_engine = self.registry.get(engine_name)
        elif len(running) == 1:
            target_engine = self.registry.get(running[0])
        elif len(running) > 1:
            print(f"[Sandbox Error] Multiple engines are running ({', '.join(running)}). Please specify which engine UI to open (e.g. agent-sandbox ui {running[0]}).", file=sys.stderr)
            return 1
        else:
            available = ", ".join(e.name for e in self.registry.list_all())
            print(f"[Sandbox Error] No engines currently running. Please specify which engine to open/start. Available engines: {available}", file=sys.stderr)
            return 1

        if not target_engine:
            print("[Sandbox Error] No engine found to open UI.", file=sys.stderr)
            return 1

        if target_engine.name not in running:
            print(f"[Sandbox] Engine '{target_engine.name}' is not running. Starting...")
            self.cmd_start([target_engine.name])

        url = target_engine.web_url
        print(f"[Sandbox] Opening {target_engine.name} Web UI ({url})...")
        if sys.platform == "darwin":
            subprocess.run(["open", url], check=False)
        else:
            if shutil.which("xdg-open"):
                subprocess.run(["xdg-open", url], check=False)
            else:
                print(f"[Sandbox] Please open {url} in your browser.")
        return 0

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
            self.cmd_start(running)
        return 0

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
            self.cmd_start(running)
        return 0

    def cmd_workspace_list(self) -> int:
        config = self.ensure_scaffolding()
        print(f"Whitelisted Workspaces ({len(config.allowed_workspaces)}):")
        for ws in config.allowed_workspaces:
            print(f"  - {ws}")
        return 0

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


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    cli = SandboxCLI()

    if not args or args[0] in ("-h", "--help", "help"):
        print("""==========================================================
  Agent Sandbox Manager CLI
==========================================================
Usage: agent-sandbox <command> [options]

Commands:
  start <engine...> [--no-host-bridge]   Start one or more engines (e.g. opencode antigravity)
  stop [engine...]                      Stop running sandbox containers and host bridge
  restart <engine...>                   Restart sandbox containers
  build [engine...]                     Rebuild the sandbox container image
  status                                Display sandbox, engine, and bridge status
  ui [engine]                           Open engine Web UI in browser (defaults to active)
  workspace add [path] [-y|--force]     Whitelist workspace directory
  workspace remove <path>               Remove workspace from whitelist
  workspace list                        List whitelisted workspaces
  rules [engine]                        Display compiled rules and sources
  skills [engine]                       Display discovered skills
  open [subpath]                        Open sandbox state folder in desktop file manager
  host-bridge [start|stop|restart|status|fg] Manage the macOS host execution bridge
==========================================================""")
        return 0

    cmd = args[0]
    rest = args[1:]

    if cmd == "start":
        engines = []
        no_hb = False
        for a in rest:
            if a == "--no-host-bridge":
                no_hb = True
            elif a == "--with-host-bridge":
                no_hb = False
            elif not a.startswith("-"):
                engines.append(a)
        return cli.cmd_start(engines, no_host_bridge=no_hb)

    elif cmd == "stop":
        engines = [a for a in rest if not a.startswith("-")]
        return cli.cmd_stop(engines)

    elif cmd == "restart":
        engines = []
        no_hb = False
        for a in rest:
            if a == "--no-host-bridge":
                no_hb = True
            elif a == "--with-host-bridge":
                no_hb = False
            elif not a.startswith("-"):
                engines.append(a)
        return cli.cmd_restart(engines, no_host_bridge=no_hb)

    elif cmd == "build":
        engines = [a for a in rest if not a.startswith("-")]
        return cli.cmd_build(engines)

    elif cmd == "status":
        return cli.cmd_status()

    elif cmd in ("ui", "web"):
        engine_name = rest[0] if rest else None
        return cli.cmd_ui(engine_name)

    elif cmd in ("workspace", "ws"):
        sub = rest[0] if rest else "list"
        sub_args = rest[1:]
        if sub == "add":
            force = "-y" in sub_args or "--force" in sub_args
            target = next((x for x in sub_args if not x.startswith("-")), None)
            return cli.cmd_workspace_add(target, force=force)
        elif sub in ("remove", "rm"):
            if not sub_args:
                print("Usage: agent-sandbox workspace remove <path>", file=sys.stderr)
                return 1
            return cli.cmd_workspace_remove(sub_args[0])
        elif sub in ("list", "ls"):
            return cli.cmd_workspace_list()
        else:
            print(f"Unknown workspace command: {sub}", file=sys.stderr)
            return 1

    elif cmd == "rules":
        engine_name = rest[0] if rest else None
        return cli.cmd_rules(engine_name)

    elif cmd == "skills":
        engine_name = rest[0] if rest else None
        return cli.cmd_skills(engine_name)

    elif cmd == "open":
        target = rest[0] if rest else None
        return cli.cmd_open(target)

    elif cmd == "host-bridge":
        sub = rest[0] if rest else "status"
        if sub in ("start", "--bg", "-d"):
            cli.start_host_bridge()
            return 0
        elif sub == "stop":
            cli.stop_host_bridge()
            return 0
        elif sub == "restart":
            cli.stop_host_bridge()
            cli.start_host_bridge()
            return 0
        elif sub == "status":
            pid = cli.get_host_bridge_pid()
            if pid:
                print(f"[Status] Host-Exec daemon: Active (PID: {pid}, Listening on port 58433)")
            else:
                print("[Status] Host-Exec daemon: Inactive")
            return 0
        elif sub in ("fg", "run"):
            daemon_script = os.path.join(cli.repo_root, "packages", "host-exec-daemon", "host_exec_daemon.py")
            env = os.environ.copy()
            env["AGENT_SANDBOX_STATE_DIR"] = cli.sandbox_dir
            env["HOST_EXEC_PORT"] = "58433"
            return subprocess.run([sys.executable, daemon_script], env=env).returncode
        else:
            print(f"Unknown host-bridge command: {sub}", file=sys.stderr)
            return 1

    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
