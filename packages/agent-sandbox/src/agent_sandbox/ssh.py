"""Host-side SSH plumbing for engines whose connection type is "ssh".

Owns the per-engine ed25519 keypair under <sandbox_dir>/<engine>/ssh/ and the
client SSH config the desktop app uses to reach the container on 127.0.0.1.
The client config is a marker-wrapped literal `Host` block prepended to the
user's `~/.ssh/config` (not an Include of a per-engine file): OpenSSH resolves
keywords first-match-wins, so the block must come before any user `Host *`
catch-all, and tools that scan `~/.ssh/config` directly (e.g. the Codex
desktop app's host picker) only see aliases written there literally.
Engine-agnostic: every helper takes the manifest and an already expanded
sandbox directory.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config.models import EngineManifest

SSH_DIRNAME = "ssh"
KEY_FILENAME = "id_ed25519"
KNOWN_HOSTS_FILENAME = "known_hosts"

#: Fixed container path where the compose generator mounts the public key so
#: the engine entrypoint can install it as authorized_keys.
CONTAINER_AUTHORIZED_KEYS = "/etc/sandbox/ssh/authorized_keys"


def _require_ssh_connection(manifest: EngineManifest) -> tuple[str, str]:
    """Validates the manifest carries a usable ssh connection; returns (user, host_alias)."""
    conn = manifest.connection
    if conn.type != "ssh":
        raise ValueError(
            f"Engine '{manifest.name}' has connection type '{conn.type}', expected 'ssh'."
        )
    if not conn.user or not conn.host_alias:
        raise ValueError(
            f"Engine '{manifest.name}' ssh connection requires 'user' and 'host_alias'."
        )
    return conn.user, conn.host_alias


def _engine_ssh_dir(manifest: EngineManifest, sandbox_dir: str | Path) -> Path:
    return Path(os.path.expanduser(str(sandbox_dir))) / manifest.name / SSH_DIRNAME


def _require_keygen() -> str:
    """Returns the ssh-keygen executable path or raises a clear RuntimeError."""
    keygen = shutil.which("ssh-keygen")
    if not keygen:
        raise RuntimeError(
            "ssh-keygen was not found on PATH. Install OpenSSH client tools to "
            "create the agent-sandbox SSH keypair."
        )
    return keygen


def ensure_ssh_keypair(manifest: EngineManifest, sandbox_dir: str | Path) -> Path:
    """Idempotently creates the per-engine ed25519 keypair; returns the public key path.

    Generates `<sandbox_dir>/<engine>/ssh/id_ed25519` (+ `.pub`) via ssh-keygen
    when missing, keeps the private key at mode 0600, and recovers a missing
    public half from an existing private key. Raises RuntimeError when
    ssh-keygen is unavailable.
    """
    _require_ssh_connection(manifest)
    ssh_dir = _engine_ssh_dir(manifest, sandbox_dir)
    key_path = ssh_dir / KEY_FILENAME
    pub_path = Path(f"{key_path}.pub")

    if not key_path.is_file():
        keygen = _require_keygen()
        ssh_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    keygen,
                    "-t", "ed25519",
                    "-N", "",
                    "-f", str(key_path),
                    "-C", f"agent-sandbox {manifest.name}",
                ],
                check=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise RuntimeError(
                f"Failed to generate SSH keypair at {key_path}: {exc}"
            ) from exc
    else:
        # Recover the public half if it was deleted out from under us.
        if not pub_path.is_file():
            keygen = _require_keygen()
            try:
                res = subprocess.run(
                    [keygen, "-y", "-f", str(key_path)],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except (OSError, subprocess.CalledProcessError) as exc:
                raise RuntimeError(
                    f"Failed to derive public key from {key_path}: {exc}"
                ) from exc
            pub_line = res.stdout.strip()
            pub_path.write_text(f"{pub_line}\n", encoding="utf-8")

    os.chmod(key_path, 0o600)
    return pub_path


def _block_markers(name: str) -> tuple[str, str]:
    """Returns the (begin, end) comment markers that bracket an engine's block."""
    return f"# >>> agent-sandbox:{name} >>>", f"# <<< agent-sandbox:{name} <<<"


def _build_host_block(manifest: EngineManifest, sandbox_dir: str | Path) -> str:
    """Renders this engine's marker-wrapped Host block (no trailing newline)."""
    user, host_alias = _require_ssh_connection(manifest)
    ssh_dir = _engine_ssh_dir(manifest, sandbox_dir)
    begin_marker, end_marker = _block_markers(manifest.name)
    return "\n".join(
        [
            begin_marker,
            f"Host {host_alias}",
            "    HostName 127.0.0.1",
            f"    Port {manifest.port}",
            f"    User {user}",
            f"    IdentityFile {ssh_dir / KEY_FILENAME}",
            "    IdentitiesOnly yes",
            "    StrictHostKeyChecking accept-new",
            f"    HostKeyAlias {host_alias}",
            f"    UserKnownHostsFile {ssh_dir / KNOWN_HOSTS_FILENAME}",
            end_marker,
        ]
    )


def _drop_engine_block(lines: list[str], name: str) -> list[str]:
    """Removes every marker block belonging to engine `name`; returns the rest.

    A block spans its begin marker through its end marker inclusive. A
    malformed block whose begin marker has no end marker drops to EOF.
    """
    begin_marker, end_marker = _block_markers(name)
    remaining = list(lines)
    while True:
        start = next(
            (i for i, line in enumerate(remaining) if line.strip() == begin_marker),
            None,
        )
        if start is None:
            return remaining
        stop = next(
            (
                i
                for i in range(start + 1, len(remaining))
                if remaining[i].strip() == end_marker
            ),
            len(remaining),
        )
        del remaining[start : stop + 1]


def _render(lines: list[str]) -> str:
    return "\n".join(lines) + "\n" if lines else ""


def _strip_leading_blank_lines(lines: list[str]) -> list[str]:
    rest = list(lines)
    while rest and not rest[0].strip():
        rest.pop(0)
    return rest


def _atomic_write_text(path: Path, content: str) -> None:
    """Writes `content` to `path` via a same-directory temp file + os.replace.

    The user's file is never truncated in place; on any failure the temp file
    is cleaned up and the original left untouched. The result is mode 0600.
    """
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _user_ssh_config_path(fs_root: Path | None) -> tuple[Path, Path]:
    """Returns the (user .ssh dir, user .ssh/config path) rooted at fs_root."""
    home = Path(fs_root) if fs_root is not None else Path.home()
    user_ssh_dir = home / ".ssh"
    return user_ssh_dir, user_ssh_dir / "config"


def ensure_ssh_client_config(
    manifest: EngineManifest,
    sandbox_dir: str | Path,
    fs_root: Path | None = None,
) -> Path:
    """Prepends this engine's Host block to the user's ~/.ssh/config.

    The block is wrapped in engine-scoped markers (`# >>> agent-sandbox:<name>
    >>>` ... `# <<< agent-sandbox:<name> <<<`) so multiple engines coexist,
    and is placed at the very top of the file: OpenSSH resolves keywords
    first-match-wins, and app host pickers that scan ~/.ssh/config directly
    only see aliases written there literally. Any previous block for this
    engine is replaced in place (idempotent, byte-stable), and all other
    content is preserved in order. The keypair is created first so the
    referenced IdentityFile exists.

    Returns the `~/.ssh/config` path (`fs_root/.ssh/config`). `fs_root`
    defaults to the real home directory, so tests can inject a temporary root
    instead of touching the actual ~/.ssh/config.
    """
    ensure_ssh_keypair(manifest, sandbox_dir)
    block = _build_host_block(manifest, sandbox_dir)

    user_ssh_dir, config_path = _user_ssh_config_path(fs_root)
    user_ssh_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(user_ssh_dir, 0o700)

    existing = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    rest = _strip_leading_blank_lines(
        _drop_engine_block(existing.splitlines(), manifest.name)
    )
    updated = f"{block}\n\n{_render(rest)}" if rest else f"{block}\n"

    if updated != existing:
        _atomic_write_text(config_path, updated)
    else:
        os.chmod(config_path, 0o600)
    return config_path


def remove_ssh_client_config(
    manifest: EngineManifest,
    sandbox_dir: str | Path,
    fs_root: Path | None = None,
) -> bool:
    """Removes this engine's marker block from the user's ~/.ssh/config.

    Uses the same tolerant block parsing as `ensure_ssh_client_config` and an
    atomic write at mode 0600. Returns True when the file changed, False when
    there was nothing to remove. `sandbox_dir` is accepted for signature
    symmetry with `ensure_ssh_client_config`; removal is keyed purely on the
    engine's markers.
    """
    del sandbox_dir
    _, config_path = _user_ssh_config_path(fs_root)
    if not config_path.is_file():
        return False

    existing = config_path.read_text(encoding="utf-8")
    lines = existing.splitlines()
    remaining = _drop_engine_block(lines, manifest.name)
    if remaining == lines:
        os.chmod(config_path, 0o600)
        return False

    _atomic_write_text(
        config_path, _render(_strip_leading_blank_lines(remaining))
    )
    return True
