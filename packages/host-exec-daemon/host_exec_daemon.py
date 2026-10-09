#!/usr/bin/env python3
"""
Antigravity Host-Exec Daemon
Runs on macOS host listening on TCP localhost.
Validates HMAC authentication tokens and whitelist policy before executing commands on host.
Supports concurrent connections, interactive approval queuing, and process lifecycle management.
"""

import asyncio
import atexit
import errno
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import signal
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

STATE_DIR = os.environ.get("AGENT_SANDBOX_STATE_DIR", os.path.expanduser("~/.agent-sandbox"))
IPC_DIR = os.path.join(STATE_DIR, "ipc")
LOGS_DIR = os.path.join(STATE_DIR, "logs")
PID_FILE_PATH = os.path.join(IPC_DIR, "host-bridge.pid")
AUTH_SECRET_PATH = os.path.join(IPC_DIR, "auth_secret.key")
WHITELIST_PATH = os.path.join(STATE_DIR, "whitelist.yaml")
HOST_EXEC_BIND = os.environ.get("HOST_EXEC_BIND", "0.0.0.0")
HOST_EXEC_PORT: Optional[int] = None
MAX_CONCURRENT_HOST_PROCESSES = int(os.environ.get("MAX_CONCURRENT_HOST_PROCESSES", "16"))


def get_required_port() -> int:
    """Read the bridge port from the runtime environment without a fallback."""
    value = os.environ.get("HOST_EXEC_PORT")
    if not value:
        raise RuntimeError("HOST_EXEC_PORT is not set")
    try:
        port = int(value)
    except ValueError as exc:
        raise RuntimeError("HOST_EXEC_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("HOST_EXEC_PORT must be between 1 and 65535")
    return port

# Ensure required state directories exist
os.makedirs(IPC_DIR, exist_ok=True)
os.makedirs(LOGS_DIR, exist_ok=True)

log_handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
try:
    log_handlers.append(logging.FileHandler(os.path.join(LOGS_DIR, "host_exec_daemon.log")))
except Exception:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=log_handlers,
)

# Global cache for dynamic hot-reloading
_cached_whitelist: Optional[Dict[str, Any]] = None
_last_loaded_mtime: float = 0.0
_last_loaded_path: Optional[str] = None

# ANSI color codes for TTY logging
ANSI_COLORS = [
    "\033[36m",  # cyan
    "\033[35m",  # magenta
    "\033[34m",  # blue
    "\033[32m",  # green
    "\033[33m",  # yellow
    "\033[96m",  # bright cyan
]
ANSI_RESET = "\033[0m"


def format_log_prefix(req_id: str, traj_id: str, cmd_name: str = "") -> str:
    """Format structured correlation tokens for request tracing."""
    short_traj = traj_id[:8] if traj_id else "manual"
    short_req = req_id[:10] if req_id else "req-init"
    tag = f"[{short_req}] [{short_traj}]"
    if cmd_name:
        tag += f" [{cmd_name}]"
    if sys.stdout.isatty():
        color = ANSI_COLORS[abs(hash(short_req)) % len(ANSI_COLORS)]
        return f"{color}{tag}{ANSI_RESET}"
    return tag


def get_or_create_secret() -> str:
    """Load or generate the shared HMAC authentication secret."""
    try:
        os.makedirs(IPC_DIR, exist_ok=True)
        os.chmod(IPC_DIR, 0o700)
    except Exception as e:
        logging.warning("Could not set permissions on %s: %s", IPC_DIR, e)

    secret = None
    if os.path.exists(AUTH_SECRET_PATH):
        try:
            with open(AUTH_SECRET_PATH, "r", encoding="utf-8") as f:
                secret = f.read().strip()
        except Exception:
            pass

    if not secret:
        secret = os.urandom(32).hex()

    try:
        with open(AUTH_SECRET_PATH, "w", encoding="utf-8") as f:
            f.write(secret)
        os.chmod(AUTH_SECRET_PATH, 0o600)
    except Exception as e:
        logging.warning("Could not write auth secret to %s: %s", AUTH_SECRET_PATH, e)

    return secret


def _read_yaml_config(filepath: str) -> Optional[Dict[str, Any]]:
    """Parse a YAML configuration file safely."""
    try:
        import yaml

        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            return None
        return yaml.safe_load(content)
    except Exception as e:
        logging.warning("Failed to parse %s: %s", filepath, e)
        return None


def get_command_description(name: str, policy: Dict[str, Any]) -> str:
    """Return explicit description or derive a human-readable fallback."""
    if policy.get("description"):
        return str(policy["description"])
    bin_path = policy.get("binary_path", "")
    bin_name = os.path.basename(bin_path) if bin_path else name
    regex = policy.get("allowed_args_regex", ".*")
    if name != bin_name:
        desc = f"Run '{bin_path}' via alias '{name}'"
    else:
        desc = f"Execute host binary '{bin_path}'"
    if regex not in [".*", "^.*$", ""]:
        clean_regex = regex.lstrip("^").rstrip("$")
        desc += f" (allowed args: {clean_regex})"
    return desc


def load_whitelist() -> Dict[str, Any]:
    """Dynamically load and hot-reload whitelist configuration from disk."""
    global _cached_whitelist, _last_loaded_mtime, _last_loaded_path

    candidate_paths = []
    env_config = os.environ.get("ANTIGRAVITY_WHITELIST_CONFIG")
    if env_config:
        candidate_paths.append(env_config)

    candidate_paths.extend([
        WHITELIST_PATH,
        os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "whitelist.default.yaml")),
    ])

    active_path = None
    for p in candidate_paths:
        if os.path.exists(p):
            active_path = p
            break

    if active_path:
        try:
            mtime = os.path.getmtime(active_path)
            if _cached_whitelist is not None and _last_loaded_path == active_path and mtime == _last_loaded_mtime:
                return _cached_whitelist
            cfg = _read_yaml_config(active_path)
            if cfg:
                _cached_whitelist = cfg
                _last_loaded_mtime = mtime
                _last_loaded_path = active_path
                logging.info("Loaded whitelist policy from %s (mtime=%s)", active_path, mtime)
                return cfg
        except Exception as e:
            logging.warning("Failed to read/stat whitelist at %s: %s", active_path, e)

    if _cached_whitelist is not None:
        return _cached_whitelist

    # Default policy fallback
    default_yaml_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config", "whitelist.default.yaml"))
    default_cfg = _read_yaml_config(default_yaml_path)
    if default_cfg:
        return default_cfg
    return {"allowed_workspaces": [], "allowed_commands": {}}


def verify_token(req_dict: Dict[str, Any], token: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature on request payload."""
    canonical_payload = json.dumps(
        {
            "command": req_dict.get("command"),
            "args": req_dict.get("args", []),
            "cwd": req_dict.get("cwd", ""),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    expected_canonical = hmac.new(
        secret.encode("utf-8"), canonical_payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_canonical, token)


def resolve_cwd(cwd: Optional[str]) -> str:
    """Resolve working directory on host, falling back to home if invalid."""
    return cwd if cwd and os.path.isdir(cwd) else os.path.expanduser("~")


COCOA_DIALOG_SCRIPT = """use AppleScript version "2.4"
use framework "Foundation"
use framework "AppKit"
use scripting additions

on run argv
    set cmd_display to item 1 of argv
    set prompt_text to item 2 of argv

    set theAlert to current application's NSAlert's alloc()'s init()
    theAlert's setMessageText:"Antigravity Host Execution Request"
    theAlert's setInformativeText:prompt_text
    theAlert's setAlertStyle:2

    theAlert's addButtonWithTitle:"Deny"
    theAlert's addButtonWithTitle:"Approve"

    set scrollView to current application's NSScrollView's alloc()'s initWithFrame:{{0, 0}, {440, 70}}
    scrollView's setHasVerticalScroller:true
    scrollView's setHasHorizontalScroller:false
    scrollView's setAutohidesScrollers:true
    scrollView's setBorderType:2

    set textView to current application's NSTextView's alloc()'s initWithFrame:{{0, 0}, {440, 70}}
    textView's setString:cmd_display
    textView's setEditable:false
    textView's setSelectable:true
    textView's setDrawsBackground:true
    textView's setTextContainerInset:{width:6, height:6}
    textView's textContainer()'s setWidthTracksTextView:true

    try
        set monoFont to current application's NSFont's monospacedSystemFontOfSize:12.0 weight:0.0
    on error
        set monoFont to current application's NSFont's userFixedPitchFontOfSize:12.0
    end try
    textView's setFont:monoFont

    scrollView's setDocumentView:textView
    theAlert's setAccessoryView:scrollView

    current application's NSApplication's sharedApplication()'s activateIgnoringOtherApps:true
    set modalResult to theAlert's runModal()
    if modalResult = 1001 then
        return "Approve"
    else
        return "Deny"
    end if
end run
"""


async def prompt_user_approval_async(
    command_name: str, args: List[str], req_id: str = "", traj_id: str = "", cwd: str = ""
) -> bool:
    """Prompt user for confirmation via native macOS dialog, showing the effective working directory."""
    cmd_display = shlex.join([command_name] + args) if isinstance(args, list) else f"{command_name} {args}"
    context_lines = []
    if cwd:
        context_lines.append(f"Working Directory: {cwd}")
    if traj_id:
        context_lines.append(f"Trajectory: {traj_id}")
    if req_id:
        context_lines.append(f"Request: {req_id}")

    prompt_parts = [
        "Antigravity Container is requesting to execute the following command on your macOS host:"
    ]
    if context_lines:
        prompt_parts.append("\n".join(context_lines))
    prompt_parts.append("Do you authorize this execution?")
    prompt_text = "\n\n".join(prompt_parts)

    # 1. Primary: Native Cocoa NSAlert with monospaced, read-only NSTextView codeblock
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", COCOA_DIALOG_SCRIPT, cmd_display, prompt_text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 and stdout_text in ("Approve", "Deny"):
            return stdout_text == "Approve"

        logging.warning(
            "Cocoa alert returned code %d (stderr: %s); falling back to display dialog",
            proc.returncode,
            stderr_bytes.decode("utf-8", errors="replace").strip(),
        )
    except Exception as e:
        logging.warning("Cocoa alert failed: %s; falling back to display dialog", e)

    # 2. Fallback: Standard AppleScript dialog
    try:
        escaped_prompt = prompt_text.replace("\\", "\\\\").replace('"', '\\"')
        escaped_cmd = cmd_display.replace("\\", "\\\\").replace('"', '\\"')
        fallback_script = (
            f'display dialog "{escaped_prompt}" '
            f'default answer "{escaped_cmd}" '
            f'with title "Antigravity Host Execution Request" '
            f'buttons {{"Deny", "Approve"}} default button "Deny" with icon caution'
        )
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", fallback_script,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, _ = await proc.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        return proc.returncode == 0 and stdout_text.startswith("button returned:Approve")
    except Exception as e:
        logging.error("Failed to prompt user via osascript fallback: %s", e)
        return False


class HostExecServer:
    """Asynchronous Host-Exec TCP Server managing concurrent request lifecycle."""

    def __init__(
        self,
        secret: str,
        max_concurrency: int = MAX_CONCURRENT_HOST_PROCESSES,
    ):
        self.secret = secret
        self.max_concurrency = max_concurrency
        self.approval_lock = asyncio.Lock()
        self.process_semaphore = asyncio.Semaphore(max_concurrency)
        self.active_processes: Dict[str, asyncio.subprocess.Process] = {}

    @staticmethod
    async def send_json(writer: asyncio.StreamWriter, payload: Dict[str, Any]) -> None:
        """Send newline-delimited JSON payload safely."""
        try:
            writer.write((json.dumps(payload) + "\n").encode("utf-8"))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def _execute_subprocess_guarded(
        self,
        cmd: List[str],
        effective_cwd: str,
        req_id: str,
        prefix: str,
        reader: asyncio.StreamReader,
        start_time: float,
    ) -> Optional[Dict[str, Any]]:
        """Spawn host subprocess protected by concurrency semaphore and client disconnect guard."""
        proc = None
        async with self.process_semaphore:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=effective_cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                self.active_processes[req_id] = proc

                async def _watch_client_disconnect() -> bool:
                    try:
                        data = await reader.read(1)
                        if not data:
                            return True
                    except Exception:
                        return True
                    return False

                disconnect_task = asyncio.create_task(_watch_client_disconnect())
                comm_task = asyncio.create_task(proc.communicate())

                done, _ = await asyncio.wait(
                    [disconnect_task, comm_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if disconnect_task in done and disconnect_task.result() is True and comm_task not in done:
                    logging.warning("%s Client disconnected while process running. Terminating child process...", prefix)
                    comm_task.cancel()
                    if proc.returncode is None:
                        try:
                            proc.terminate()
                            await proc.wait()
                        except Exception:
                            pass
                    return None

                if not disconnect_task.done():
                    disconnect_task.cancel()
                    try:
                        await disconnect_task
                    except (asyncio.CancelledError, Exception):
                        pass

                stdout_bytes, stderr_bytes = comm_task.result()
                duration_ms = (time.monotonic() - start_time) * 1000
                logging.info("%s Finished in %.1fms (exit=%d)", prefix, duration_ms, proc.returncode)

                return {
                    "status": "success",
                    "returncode": proc.returncode,
                    "stdout": stdout_bytes.decode("utf-8", errors="replace"),
                    "stderr": stderr_bytes.decode("utf-8", errors="replace"),
                }
            except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
                logging.warning("%s Execution cancelled. Terminating child process...", prefix)
                if proc and proc.returncode is None:
                    try:
                        proc.terminate()
                        await proc.wait()
                    except Exception:
                        pass
                raise
            finally:
                self.active_processes.pop(req_id, None)

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Handle individual client connection from reading request to writing response."""
        req_id = f"req-{os.urandom(4).hex()}"
        traj_id = ""
        command_name = ""

        try:
            raw_line = await reader.readline()
            if not raw_line:
                return

            raw_data = raw_line.decode("utf-8", errors="replace").strip()
            if not raw_data:
                return

            try:
                req = json.loads(raw_data)
            except Exception as e:
                logging.warning("[%s] Malformed JSON request: %s", req_id, e)
                await self.send_json(writer, {"status": "error", "message": f"Malformed JSON: {e}"})
                return

            command_name = str(req.get("command", ""))
            raw_args = req.get("args", [])
            args = [str(a) for a in raw_args] if isinstance(raw_args, list) else []
            token = str(req.get("token", ""))
            raw_cwd = req.get("cwd")
            req_id = str(req.get("request_id") or req_id)
            traj_id = str(req.get("trajectory_id", ""))

            prefix = format_log_prefix(req_id, traj_id, command_name)
            whitelist = load_whitelist()

            # 1. HMAC Token Verification
            if not verify_token(req, token, self.secret):
                logging.warning("%s HMAC Token verification failed!", prefix)
                await self.send_json(writer, {"status": "error", "message": "Authentication failed: invalid HMAC token"})
                return

            # 2. Introspection Capabilities Query (__list__)
            if command_name in ["__list__", "__capabilities__"]:
                logging.info("%s Handled introspection capabilities request (__list__)", prefix)
                commands_summary = {}
                for c_name, policy in whitelist.get("allowed_commands", {}).items():
                    commands_summary[c_name] = {
                        "binary_path": policy.get("binary_path", ""),
                        "allowed_args_regex": policy.get("allowed_args_regex", ".*"),
                        "require_interactive_approval": policy.get("require_interactive_approval", False),
                        "description": get_command_description(c_name, policy),
                    }
                resp = {
                    "status": "success",
                    "type": "capabilities",
                    "allowed_workspaces": whitelist.get("allowed_workspaces", []),
                    "allowed_commands": commands_summary,
                }
                await self.send_json(writer, resp)
                return

            full_cmd_str = f"{command_name} {shlex.join(args)}".strip()
            logging.info("%s Received host execution request: %s", prefix, full_cmd_str)

            # 3. Whitelist Policy Verification
            policies = whitelist.get("allowed_commands", {})
            if command_name not in policies:
                logging.warning("%s Command '%s' is not in host whitelist!", prefix, command_name)
                await self.send_json(writer, {
                    "status": "error",
                    "error_type": "not_whitelisted",
                    "message": f"Command '{command_name}' is not whitelisted on host (~/.agent-sandbox/whitelist.yaml)",
                })
                return

            policy = policies[command_name]
            bin_path = policy["binary_path"]
            args_regex = policy.get("allowed_args_regex", ".*")
            args_str = shlex.join(args) if args else ""

            # 4. Arguments Regex Validation
            if not re.fullmatch(args_regex, args_str):
                logging.warning("%s Arguments '%s' violated policy regex: %s", prefix, args_str, args_regex)
                await self.send_json(writer, {
                    "status": "error",
                    "error_type": "args_violation",
                    "message": f"Command arguments '{args_str}' violated whitelist pattern ({args_regex}) in ~/.agent-sandbox/whitelist.yaml",
                })
                return

            # 5. Resolve Effective Working Directory on Host (home fallback if path is absent)
            effective_cwd = resolve_cwd(raw_cwd)

            # 6. Interactive User Approval (Serialized via approval_lock)
            if policy.get("require_interactive_approval", False):
                logging.info("%s Command requires interactive approval. Waiting for approval lock...", prefix)
                async with self.approval_lock:
                    logging.info("%s Displaying approval dialog on macOS...", prefix)
                    approved = await prompt_user_approval_async(
                        command_name, args, req_id, traj_id, cwd=effective_cwd
                    )

                if not approved:
                    logging.warning("%s Execution denied by user.", prefix)
                    await self.send_json(writer, {
                        "status": "error",
                        "error_type": "denied_by_user",
                        "message": "Execution denied by user via macOS approval dialog",
                    })
                    return
                logging.info("%s Execution approved by user.", prefix)

            # 7. Subprocess Execution Guarded by Concurrency Semaphore & Disconnect Watcher
            cmd = [bin_path] + args
            start_time = time.monotonic()
            logging.info("%s Executing '%s' with cwd='%s'", prefix, bin_path, effective_cwd)

            resp_payload = await self._execute_subprocess_guarded(
                cmd, effective_cwd, req_id, prefix, reader, start_time
            )
            if resp_payload is not None:
                await self.send_json(writer, resp_payload)

        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            prefix = format_log_prefix(req_id, traj_id, command_name)
            logging.error("%s Error handling request: %s", prefix, e)
            await self.send_json(writer, {"status": "error", "message": str(e)})
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    def terminate_active_processes(self) -> None:
        """Terminate all active child processes."""
        for req_id, proc in list(self.active_processes.items()):
            if proc.returncode is None:
                try:
                    proc.terminate()
                except Exception:
                    pass
        self.active_processes.clear()


# Default singleton server instance for process lifecycle and backwards compatibility
_default_server: Optional[HostExecServer] = None
_active_processes: Dict[str, asyncio.subprocess.Process] = {}
_approval_lock: Optional[asyncio.Lock] = None
_process_semaphore: Optional[asyncio.Semaphore] = None


def get_default_server(secret: str = None) -> HostExecServer:
    """Get or initialize the default HostExecServer singleton."""
    global _default_server, _active_processes, _approval_lock, _process_semaphore
    if _default_server is None:
        sec = secret or get_or_create_secret()
        _default_server = HostExecServer(sec)
        _active_processes = _default_server.active_processes
        _approval_lock = _default_server.approval_lock
        _process_semaphore = _default_server.process_semaphore
    return _default_server


async def handle_client_async(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, secret: str):
    """Compatibility entrypoint delegating to HostExecServer."""
    server = get_default_server(secret)
    await server.handle_client(reader, writer)


def cleanup():
    """Clean up PID file on shutdown."""
    if os.path.exists(PID_FILE_PATH):
        try:
            with open(PID_FILE_PATH, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content and int(content) == os.getpid():
                os.remove(PID_FILE_PATH)
        except Exception:
            pass


atexit.register(cleanup)


def terminate_all_active_processes():
    """Terminate all active child processes on shutdown."""
    global _default_server
    if _default_server is not None:
        _default_server.terminate_active_processes()


async def main_async():
    """Asynchronous entrypoint running TCP server."""
    global HOST_EXEC_PORT
    HOST_EXEC_PORT = get_required_port()
    secret = get_or_create_secret()
    server_instance = get_default_server(secret)
    initial_whitelist = load_whitelist()

    tcp_server = await asyncio.start_server(
        server_instance.handle_client,
        HOST_EXEC_BIND,
        HOST_EXEC_PORT,
    )

    try:
        with open(PID_FILE_PATH, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except Exception as e:
        logging.warning("Could not write PID file to %s: %s", PID_FILE_PATH, e)

    num_cmds = len(initial_whitelist.get("allowed_commands", {}))
    logging.info(
        "Host-Exec Daemon is active on TCP %s:%s with %d whitelisted commands (max concurrency=%d).",
        HOST_EXEC_BIND,
        HOST_EXEC_PORT,
        num_cmds,
        server_instance.max_concurrency,
    )

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    shutdown_count = 0

    def signal_handler():
        nonlocal shutdown_count
        shutdown_count += 1
        if shutdown_count > 1:
            logging.critical("Forced shutdown requested. Exiting immediately.")
            server_instance.terminate_active_processes()
            sys.exit(1)
        logging.info("Received shutdown signal. Stopping daemon...")
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: stop_event.set())

    async with tcp_server:
        await stop_event.wait()

    logging.info("Shutting down active processes and closing server...")
    server_instance.terminate_active_processes()
    tcp_server.close()
    await tcp_server.wait_closed()
    cleanup()
    logging.info("Host-Exec Daemon shutdown complete.")


def main():
    try:
        asyncio.run(main_async())
    except (KeyboardInterrupt, SystemExit):
        terminate_all_active_processes()
        cleanup()
    except OSError as e:
        if e.errno in (errno.EADDRINUSE, 48, 98):
            logging.error(
                "Cannot bind Host-Exec Daemon to %s:%s: address already in use.",
                HOST_EXEC_BIND,
                HOST_EXEC_PORT,
            )
            sys.exit(1)
        raise


if __name__ == "__main__":
    main()
