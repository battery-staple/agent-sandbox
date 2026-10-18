#!/usr/bin/env python3
"""
Agent Sandbox Host-Exec Daemon
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
# The developer's home directory inside the sandbox container. The client reports
# paths under this root, so the daemon translates them onto the host home
# directory (for example ``/home/developer/xyz`` -> ``~/xyz``).
CONTAINER_HOME = os.environ.get("HOST_EXEC_CONTAINER_HOME", "/home/developer")
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
    if name != bin_name:
        desc = f"Run '{bin_path}' via alias '{name}'"
    else:
        desc = f"Execute host binary '{bin_path}'"

    regex = policy.get("auto_args_regex") or policy.get("approval_args_regex") or ".*"
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


def translate_container_path(
    path: Optional[str],
    container_home: Optional[str] = None,
    host_home: Optional[str] = None,
) -> Optional[str]:
    """Map a path inside the container home onto the equivalent host path.

    Inside the sandbox the developer's home is ``/home/developer``, while on the
    host it is the platform user's home directory. A container path such as
    ``/home/developer/project`` therefore corresponds to ``~/project`` on the
    host.

    Returns the translated path, or ``None`` when ``path`` does not live inside
    the container home directory, or when the translation would escape the host
    home directory. Such paths need no translation.
    """
    if not isinstance(path, str) or not path:
        return None

    container_root = (container_home or CONTAINER_HOME).rstrip("/") or "/"
    host_root = (host_home or os.path.expanduser("~")).rstrip("/") or "/"

    if path == container_root:
        return host_root

    prefix = "/" if container_root == "/" else container_root + "/"
    if not path.startswith(prefix):
        return None

    translated = os.path.normpath(os.path.join(host_root, path[len(prefix):]))
    # Confine the result to the host home directory. A crafted path such as
    # ``/home/developer/../etc`` or a doubled slash must not escape it.
    if (
        host_root != "/"
        and translated != host_root
        and not translated.startswith(host_root + os.sep)
    ):
        return None
    return translated


def resolve_cwd(
    cwd: Optional[str],
    prefix: str = "",
    container_home: Optional[str] = None,
    host_home: Optional[str] = None,
) -> str:
    """Resolve the host working directory for an in-container request.

    ``cwd`` is the directory reported by the in-container client. It is used
    verbatim when the host can already see it, for example a workspace mounted at
    the same path. Otherwise a path under the container home is translated onto
    the host home directory (``/home/developer/xyz`` -> ``~/xyz``).

    When neither the requested directory nor its translation is usable on the
    host (for example a container-only worktree path), the command runs in the
    host home directory instead. That silent mismatch is logged as a warning
    naming both the in-container directory and the out-of-container working
    directory.
    """
    resolved_host_home = host_home or os.path.expanduser("~")

    if isinstance(cwd, str) and cwd and os.path.isdir(cwd):
        return cwd

    if cwd:
        translated = translate_container_path(cwd, container_home, resolved_host_home)
        if translated and os.path.isdir(translated):
            tag = f"{prefix} " if prefix else ""
            logging.info(
                "%sIn-container directory '%s' translated to host directory '%s'",
                tag,
                cwd,
                translated,
            )
            return translated

    if cwd:
        tag = f"{prefix} " if prefix else ""
        logging.warning(
            "%sIn-container directory '%s' is unavailable on the host; "
            "using out-of-container working directory '%s'",
            tag,
            cwd,
            resolved_host_home,
        )
    return resolved_host_home


COCOA_DIALOG_SCRIPT = """use AppleScript version "2.4"
use framework "Foundation"
use framework "AppKit"
use scripting additions

property modalResult : "Deny"

on run argv
    if not (current application's NSThread's isMainThread()) as boolean then
        my performSelectorOnMainThread:"showAlert:" withObject:argv waitUntilDone:true
    else
        my showAlert(argv)
    end if
    return modalResult
end run

on showAlert(argv)
    set cmd_display to item 1 of argv
    set prompt_text to item 2 of argv

    set curApp to current application's NSApplication's sharedApplication()
    try
        curApp's setActivationPolicy:(current application's NSApplicationActivationPolicyAccessory)
    on error
        try
            curApp's setActivationPolicy:1
        end try
    end try

    set theAlert to current application's NSAlert's alloc()'s init()
    theAlert's setMessageText:"Host Execution Request"
    theAlert's setInformativeText:prompt_text
    theAlert's setAlertStyle:1

    -- Icon: Use system terminal icon instead of generic application folder
    try
        set sfIcon to current application's NSImage's imageWithSystemSymbolName:"terminal.fill" accessibilityDescription:"Host Exec"
        if sfIcon is not missing value then
            theAlert's setIcon:sfIcon
        end if
    end try

    set denyBtn to theAlert's addButtonWithTitle:"Deny"
    set approveBtn to theAlert's addButtonWithTitle:"Approve"

    try
        denyBtn's setKeyEquivalent:(character id 27)
    end try
    try
        approveBtn's setKeyEquivalent:(character id 13)
    end try

    -- Dynamic height for code block
    set cmdLen to length of cmd_display
    set boxH to 60
    if cmdLen > 65 then
        set boxH to 88
    end if
    if cmdLen > 150 then
        set boxH to 130
    end if

    set scrollView to current application's NSScrollView's alloc()'s initWithFrame:{{0, 0}, {580, boxH}}
    scrollView's setHasVerticalScroller:true
    scrollView's setHasHorizontalScroller:false
    scrollView's setAutohidesScrollers:true
    scrollView's setBorderType:1

    try
        scrollView's setDrawsBackground:true
        scrollView's setBackgroundColor:(current application's NSColor's controlBackgroundColor())
        scrollView's setWantsLayer:true
        scrollView's |layer|()'s setCornerRadius:8.0
    end try

    set textView to current application's NSTextView's alloc()'s initWithFrame:{{0, 0}, {580, boxH}}
    textView's setString:cmd_display
    textView's setEditable:false
    textView's setSelectable:true
    textView's setDrawsBackground:false
    try
        textView's setTextColor:(current application's NSColor's labelColor())
    end try
    try
        textView's setTextContainerInset:{9, 12}
    end try

    -- Modern typography: SFMono-Medium 14pt, fall back gracefully
    try
        set monoFont to current application's NSFont's fontWithName:"SFMono-Medium" |size|:14.0
        if monoFont is missing value then error
    on error
        try
            set monoFont to current application's NSFont's fontWithName:"SF Mono" |size|:14.0
            if monoFont is missing value then error
        on error
            try
                set monoFont to current application's NSFont's monospacedSystemFontOfSize:14.0 weight:(current application's NSFontWeightMedium)
                if monoFont is missing value then error
            on error
                try
                    set monoFont to current application's NSFont's monospacedSystemFontOfSize:12.0 weight:0.0
                on error
                    try
                        set monoFont to current application's NSFont's fontWithName:"Menlo-Bold" |size|:13.0
                    on error
                        set monoFont to current application's NSFont's userFixedPitchFontOfSize:13.0
                    end try
                end try
            end try
        end try
    end try
    textView's setFont:monoFont

    scrollView's setDocumentView:textView
    theAlert's setAccessoryView:scrollView

    -- Layout to calculate window geometry
    theAlert's layout()
    set alertWin to theAlert's |window|()

    -- Apply rich typography to the informative text field
    try
        set attrStr to current application's NSMutableAttributedString's alloc()'s initWithString:""
        set boldFont to current application's NSFont's boldSystemFontOfSize:17.0
        set regularFont to current application's NSFont's systemFontOfSize:17.0
        
        set monoFont11 to current application's NSFont's fontWithName:"SFMono-Regular" |size|:15.5
        if monoFont11 is missing value then
            try
                set monoFont11 to current application's NSFont's monospacedSystemFontOfSize:15.5 weight:0.0
            on error
                set monoFont11 to current application's NSFont's userFixedPitchFontOfSize:15.5
            end try
        end if

        set labelCol to current application's NSColor's secondaryLabelColor()
        set valueCol to current application's NSColor's labelColor()
        set noteCol to current application's NSColor's systemOrangeColor()
        set pathBgCol to current application's NSColor's colorWithWhite:0.0 alpha:0.4

        set labelDict to current application's NSDictionary's dictionaryWithObjects:{boldFont, labelCol} forKeys:{current application's NSFontAttributeName, current application's NSForegroundColorAttributeName}
        set valueDict to current application's NSDictionary's dictionaryWithObjects:{regularFont, valueCol} forKeys:{current application's NSFontAttributeName, current application's NSForegroundColorAttributeName}
        try
            set monoDict to current application's NSDictionary's dictionaryWithObjects:{monoFont11, valueCol, pathBgCol, 1.5} forKeys:{current application's NSFontAttributeName, current application's NSForegroundColorAttributeName, current application's NSBackgroundColorAttributeName, current application's NSBaselineOffsetAttributeName}
        on error
            set monoDict to current application's NSDictionary's dictionaryWithObjects:{monoFont11, valueCol, pathBgCol} forKeys:{current application's NSFontAttributeName, current application's NSForegroundColorAttributeName, current application's NSBackgroundColorAttributeName}
        end try
        set noteDict to current application's NSDictionary's dictionaryWithObjects:{boldFont, noteCol} forKeys:{current application's NSFontAttributeName, current application's NSForegroundColorAttributeName}

        set titleFont to current application's NSFont's boldSystemFontOfSize:24.0
        set titleDict to current application's NSDictionary's dictionaryWithObjects:{titleFont, valueCol} forKeys:{current application's NSFontAttributeName, current application's NSForegroundColorAttributeName}
        set titlePart to current application's NSAttributedString's alloc()'s initWithString:"Host Execution Request" attributes:titleDict

        set promptParas to paragraphs of prompt_text
        repeat with aPara in promptParas
            set lineText to (aPara as text)
            if length of lineText > 0 then
                if (attrStr's |length|()) > 0 then
                    set nl to current application's NSAttributedString's alloc()'s initWithString:(character id 10) attributes:valueDict
                    attrStr's appendAttributedString:nl
                end if

                set colonOffset to offset of ": " in lineText
                if colonOffset > 0 then
                    set kStr to (text 1 through (colonOffset + 1) of lineText)
                    set vStr to (text (colonOffset + 2) through -1 of lineText)
                    
                    if kStr starts with "Note" then
                        set kPart to current application's NSAttributedString's alloc()'s initWithString:kStr attributes:noteDict
                        set vPart to current application's NSAttributedString's alloc()'s initWithString:vStr attributes:noteDict
                    else
                        set kPart to current application's NSAttributedString's alloc()'s initWithString:kStr attributes:labelDict
                        if kStr contains "Directory" or kStr contains "Request" or kStr contains "Trajectory" then
                            set vPart to current application's NSAttributedString's alloc()'s initWithString:vStr attributes:monoDict
                        else
                            set vPart to current application's NSAttributedString's alloc()'s initWithString:vStr attributes:valueDict
                        end if
                    end if
                    attrStr's appendAttributedString:kPart
                    attrStr's appendAttributedString:vPart
                else
                    set pPart to current application's NSAttributedString's alloc()'s initWithString:lineText attributes:valueDict
                    attrStr's appendAttributedString:pPart
                end if
            end if
        end repeat

        -- First pass: Set attributed informative text and configure wrapping
        repeat with aView in (alertWin's contentView()'s subviews())
            if (aView's isKindOfClass:(current application's NSTextField's |class|())) as boolean then
                set curVal to (aView's stringValue() as text)
                if not (curVal contains "Host Execution") then
                    try
                        aView's setAllowsEditingTextAttributes:true
                        aView's cell()'s setWraps:true
                        aView's setPreferredMaxLayoutWidth:580
                        aView's setAttributedStringValue:attrStr
                    end try
                end if
            end if
        end repeat

        -- Re-layout so NSAlert wraps informative text and recalculates window dimensions
        theAlert's layout()

        -- Second pass: Apply 24pt bold title after layout so NSAlert layout does not reset it
        repeat with aView in (alertWin's contentView()'s subviews())
            if (aView's isKindOfClass:(current application's NSTextField's |class|())) as boolean then
                set curVal to (aView's stringValue() as text)
                if curVal contains "Host Execution" then
                    try
                        aView's setAllowsEditingTextAttributes:true
                        aView's setAttributedStringValue:titlePart
                        aView's setFont:titleFont
                        aView's sizeToFit()
                    end try
                end if
            end if
        end repeat
    end try

    -- Window Level: NSStatusWindowLevel = 25 (floats above normal and modal windows)
    try
        alertWin's setLevel:(current application's NSStatusWindowLevel)
    on error
        try
            alertWin's setLevel:25
        end try
    end try

    -- Collection Behavior: CanJoinAllSpaces (1) + FullScreenAuxiliary (256) = 257
    try
        alertWin's setCollectionBehavior:257
    end try

    try
        alertWin's setHidesOnDeactivate:false
    end try

    -- Position at top-right of active screen (screen with mouse cursor)
    try
        set mouseLoc to current application's NSEvent's mouseLocation()
        set allScreens to current application's NSScreen's screens()
        set targetScreen to current application's NSScreen's mainScreen()

        set mouseX to 0
        set mouseY to 0
        try
            set mouseX to x of mouseLoc
            set mouseY to y of mouseLoc
        on error
            try
                set mouseX to item 1 of mouseLoc
                set mouseY to item 2 of mouseLoc
            end try
        end try

        repeat with aScreen in allScreens
            set sFrame to aScreen's frame()
            set sx to 0
            set sy to 0
            set sw to 0
            set sh to 0
            try
                set sx to x of origin of sFrame
                set sy to y of origin of sFrame
                set sw to width of size of sFrame
                set sh to height of size of sFrame
            on error
                try
                    set sx to item 1 of item 1 of sFrame
                    set sy to item 2 of item 1 of sFrame
                    set sw to item 1 of item 2 of sFrame
                    set sh to item 2 of item 2 of sFrame
                end try
            end try
            if mouseX >= sx and mouseX <= (sx + sw) and mouseY >= sy and mouseY <= (sy + sh) then
                set targetScreen to aScreen
                exit repeat
            end if
        end repeat

        set vFrame to targetScreen's visibleFrame()
        set vx to 0
        set vy to 0
        set vw to 0
        set vh to 0
        try
            set vx to x of origin of vFrame
            set vy to y of origin of vFrame
            set vw to width of size of vFrame
            set vh to height of size of vFrame
        on error
            try
                set vx to item 1 of item 1 of vFrame
                set vy to item 2 of item 1 of vFrame
                set vw to item 1 of item 2 of vFrame
                set vh to item 2 of item 2 of vFrame
            end try
        end try

        set winFrame to alertWin's frame()
        set winW to 580
        set winH to 280
        try
            set winW to width of size of winFrame
            set winH to height of size of winFrame
        on error
            try
                set winW to item 1 of item 2 of winFrame
                set winH to item 2 of item 2 of winFrame
            end try
        end try

        set posX to vx + vw - winW - 24
        set posY to vy + vh - winH - 24
        try
            alertWin's setFrameOrigin:{posX, posY}
        on error
            try
                alertWin's setFrameOrigin:{x:posX, y:posY}
            end try
        end try
    end try

    -- Play alert chime
    try
        current application's NSBeep()
    end try

    try
        alertWin's orderFrontRegardless()
    end try

    alertWin's makeKeyAndOrderFront:missing value
    try
        curApp's activateIgnoringOtherApps:true
    end try
    set modalRes to curApp's runModalForWindow:alertWin
    alertWin's orderOut:missing value

    if modalRes = 1001 then
        set modalResult to "Approve"
    else
        set modalResult to "Deny"
    end if
end showAlert
"""


async def prompt_user_approval_async(
    command_name: str,
    args: List[str],
    req_id: str = "",
    traj_id: str = "",
    cwd: str = "",
    container_cwd: str = "",
    note: str = "",
) -> bool:
    """Prompt user for confirmation, showing both working directories.

    ``container_cwd`` is the directory the client reported inside the container;
    ``cwd`` is the effective directory on the host where the command will run.
    """
    cmd_display = shlex.join([command_name] + args) if isinstance(args, list) else f"{command_name} {args}"
    context_lines = []
    if note:
        context_lines.append(f"Note: {note}")
    if container_cwd:
        context_lines.append(f"In-Container Directory: {container_cwd}")
        if cwd:
            context_lines.append(f"Out-of-Container Working Directory: {cwd}")
    elif cwd:
        context_lines.append(f"Working Directory: {cwd}")
    if traj_id:
        context_lines.append(f"Trajectory: {traj_id}")
    if req_id:
        context_lines.append(f"Request: {req_id}")

    prompt_parts = []
    if context_lines:
        prompt_parts.append("\n".join(context_lines))
    prompt_text = "\n\n".join(prompt_parts) if prompt_parts else "Do you authorize this execution?"

    # 1. Primary: Native Cocoa NSAlert with monospaced, read-only NSTextView codeblock
    stderr_text = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            "osascript", "-e", COCOA_DIALOG_SCRIPT, cmd_display, prompt_text,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout_text = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0 and stdout_text in ("Approve", "Deny"):
            return stdout_text == "Approve"

        logging.warning(
            "Cocoa alert returned code %d (stderr: %s); falling back to display dialog",
            proc.returncode,
            stderr_text,
        )
    except Exception as e:
        stderr_text = str(e)
        logging.warning("Cocoa alert failed: %s; falling back to display dialog", e)

    # 2. Fallback: Standard AppleScript dialog
    try:
        escaped_prompt = prompt_text.replace("\\", "\\\\").replace('"', '\\"')
        if stderr_text:
            cleaned_err = stderr_text.replace("\\", "\\\\").replace('"', '\\"')
            escaped_prompt += f"\\n\\n[Cocoa Alert Error: {cleaned_err}]"
        escaped_cmd = cmd_display.replace("\\", "\\\\").replace('"', '\\"')
        fallback_script = (
            f'display dialog "{escaped_prompt}" '
            f'default answer "{escaped_cmd}" '
            f'with title "Agent Sandbox Host Execution Request" '
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
                    cmd_info = {
                        "binary_path": policy.get("binary_path", ""),
                        "description": get_command_description(c_name, policy),
                    }
                    if "auto_args_regex" in policy:
                        cmd_info["auto_args_regex"] = policy["auto_args_regex"]
                    if "approval_args_regex" in policy:
                        cmd_info["approval_args_regex"] = policy["approval_args_regex"]
                    commands_summary[c_name] = cmd_info
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
            args_str = shlex.join(args) if args else ""

            auto_regex = policy.get("auto_args_regex")
            approval_regex = policy.get("approval_args_regex")

            approval_note = ""
            auto_matches = bool(auto_regex and re.fullmatch(auto_regex, args_str))
            approval_matches = bool(approval_regex and re.fullmatch(approval_regex, args_str))

            if approval_matches and auto_matches:
                logging.warning(
                    "%s Arguments '%s' matched both approval_args_regex and auto_args_regex; defaulting to requiring approval",
                    prefix,
                    args_str,
                )
                needs_approval = True
                approval_note = "Command arguments matched both approval_args_regex and auto_args_regex (approval required as fail-safe)."
            elif approval_matches:
                needs_approval = True
            elif auto_matches:
                needs_approval = False
            else:
                logging.warning("%s Arguments '%s' violated whitelist policy", prefix, args_str)
                await self.send_json(writer, {
                    "status": "error",
                    "error_type": "args_violation",
                    "message": f"Command arguments '{args_str}' violated whitelist pattern in ~/.agent-sandbox/whitelist.yaml",
                })
                return

            # 5. Resolve Effective Working Directory on Host (home fallback if path is absent,
            #    warning with both the in-container and out-of-container directory)
            effective_cwd = resolve_cwd(raw_cwd, prefix)

            # 6. Interactive User Approval (Serialized via approval_lock)
            if needs_approval:
                logging.info("%s Command requires interactive approval. Waiting for approval lock...", prefix)
                async with self.approval_lock:
                    logging.info("%s Displaying approval dialog on macOS...", prefix)
                    approved = await prompt_user_approval_async(
                        command_name,
                        args,
                        req_id,
                        traj_id,
                        cwd=effective_cwd,
                        container_cwd=raw_cwd or "",
                        note=approval_note,
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
