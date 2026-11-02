"""Check host-exec endpoint validation and its handoff across a real SSH session."""

import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest


REPO_ROOT = Path(__file__).resolve().parents[3]
RENDERER = REPO_ROOT / "engines/codex/ssh-environment.py"


def render(host="bridge.example", port="58433"):
    env = os.environ.copy()
    for name, value in (("HOST_EXEC_HOST", host), ("HOST_EXEC_PORT", port)):
        env.pop(name, None)
        if value is not None:
            env[name] = value
    return subprocess.run(
        [sys.executable, str(RENDERER)], env=env, capture_output=True, text=True, check=False,
    )


class TestCodexSshEnvironment(unittest.TestCase):
    def test_invalid_endpoint_is_rejected_without_emitting_config(self):
        endpoints = [
            (None, "58433"), ("", "58433"), ("bridge\nPermitRootLogin yes", "58433"),
            ('bridge"', "58433"), ("bridge name", "58433"),
            ("bridge", None), ("bridge", ""), ("bridge", "0"), ("bridge", "65536"),
            ("bridge", "abc"), ("bridge", "58433\nPermitRootLogin yes"),
        ]
        for host, port in endpoints:
            with self.subTest(host=host, port=port):
                result = render(host, port)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("[Codex Error] HOST_EXEC_", result.stderr)

    @unittest.skipUnless(
        shutil.which("sshd") and shutil.which("ssh") and shutil.which("ssh-keygen")
        and Path("/run/sshd").is_dir(),
        "requires OpenSSH server/client and the Linux sshd runtime directory",
    )
    def test_configured_endpoint_reaches_noninteractive_ssh_session(self):
        # Use a private daemon and throwaway keys; never modify the running server.
        with tempfile.TemporaryDirectory(prefix="codex-ssh-test-") as tmp:
            root = Path(tmp)
            key = root / "key"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True,
            )
            username = pwd.getpwuid(os.getuid()).pw_name
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = listener.getsockname()[1]
            config = root / "sshd_config"
            endpoint_config = root / "endpoint.conf"
            config.write_text(
                f"Include {endpoint_config}\n"
                f"Port {port}\nListenAddress 127.0.0.1\nHostKey {key}\n"
                f"PidFile {root / 'sshd.pid'}\nAuthorizedKeysFile {key}.pub\n"
                f"AllowUsers {username}\n"
                "UsePAM no\nStrictModes no\nPasswordAuthentication no\n"
                "KbdInteractiveAuthentication no\nPrintMotd no\nLogLevel ERROR\n"
            )
            # Changing the container endpoint must change the next SSH session too.
            for host, bridge_port in (("bridge.example", "12345"), ("127.0.0.1", "65535")):
                with self.subTest(host=host, port=bridge_port):
                    result = render(host, bridge_port)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    endpoint_config.write_text(result.stdout)
                    sshd = str(Path(shutil.which("sshd")).resolve())
                    subprocess.run([sshd, "-t", "-f", str(config)], check=True)
                    # Keep the endpoint out of the daemon's environment as well.
                    env = {k: v for k, v in os.environ.items() if not k.startswith("HOST_EXEC_")}
                    with (root / "sshd.log").open("w+") as log:
                        server = subprocess.Popen([sshd, "-D", "-e", "-f", str(config)], env=env, stderr=log)
                        try:
                            deadline = time.monotonic() + 5
                            while True:
                                try:
                                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                                        break
                                except OSError:
                                    if server.poll() is not None or time.monotonic() >= deadline:
                                        log.seek(0)
                                        self.fail(f"Test sshd did not start: {log.read()}")
                                    time.sleep(0.05)
                            session = subprocess.run(
                                ["ssh", "-F", "/dev/null", "-i", str(key), "-p", str(port),
                                 "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no",
                                 "-o", "UserKnownHostsFile=/dev/null", "-o", "IdentitiesOnly=yes",
                                 "-o", "ConnectTimeout=3", f"{username}@127.0.0.1",
                                 "printenv HOST_EXEC_HOST HOST_EXEC_PORT"],
                                capture_output=True, text=True, timeout=10,
                            )
                            self.assertEqual(session.returncode, 0, session.stderr)
                            self.assertEqual(session.stdout.splitlines(), [host, bridge_port])
                        finally:
                            server.terminate()
                            server.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
