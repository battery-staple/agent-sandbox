"""Render the container's host-exec endpoint for OpenSSH session environments."""

import os
import re
import sys


def main() -> int:
    host = os.environ.get("HOST_EXEC_HOST", "")
    port = os.environ.get("HOST_EXEC_PORT", "")
    # Restrict values to endpoint characters before placing them in sshd config.
    # In particular, reject whitespace, quotes, and newlines (config injection).
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", host):
        print("[Codex Error] HOST_EXEC_HOST must be a nonempty hostname or IP address", file=sys.stderr)
        return 1
    if not re.fullmatch(r"[0-9]{1,5}", port) or not 1 <= int(port) <= 65535:
        print("[Codex Error] HOST_EXEC_PORT must be an integer between 1 and 65535", file=sys.stderr)
        return 1

    print("# Generated at container startup; sshd does not inherit these into sessions.")
    print(f"SetEnv HOST_EXEC_HOST={host} HOST_EXEC_PORT={port}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
