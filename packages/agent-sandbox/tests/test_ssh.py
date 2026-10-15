"""Tests for host-side SSH helpers in agent_sandbox.ssh."""
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.config.models import EngineConnectionConfig, EngineManifest
from agent_sandbox.ssh import (
    ensure_ssh_client_config,
    ensure_ssh_keypair,
    remove_ssh_client_config,
)


def _ssh_manifest(name: str = "fake", port: int = 1234) -> EngineManifest:
    return EngineManifest(
        name=name,
        port=port,
        connection=EngineConnectionConfig(
            type="ssh", user="developer", host_alias=f"agent-sandbox-{name}"
        ),
    )


class TestEnsureSshKeypair(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_ssh_key_")
        self.sandbox_dir = os.path.join(self.tmpdir, "sandbox")
        self.manifest = _ssh_manifest()
        self.key_path = Path(self.sandbox_dir) / "fake" / "ssh" / "id_ed25519"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_creates_keypair_with_expected_permissions(self):
        pub = ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        self.assertEqual(pub, Path(f"{self.key_path}.pub"))
        self.assertTrue(self.key_path.is_file())
        self.assertTrue(pub.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(self.key_path).st_mode), 0o600)
        self.assertIn("OPENSSH PRIVATE KEY", self.key_path.read_text(encoding="utf-8"))
        self.assertTrue(pub.read_text(encoding="utf-8").startswith("ssh-ed25519 "))

    def test_second_call_is_idempotent(self):
        pub = ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        before = (os.stat(self.key_path).st_ino, os.stat(self.key_path).st_mtime_ns)
        pub_again = ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        after = (os.stat(self.key_path).st_ino, os.stat(self.key_path).st_mtime_ns)
        self.assertEqual(pub, pub_again)
        self.assertEqual(before, after)

    def test_restores_private_key_permissions(self):
        ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        os.chmod(self.key_path, 0o644)
        ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        self.assertEqual(stat.S_IMODE(os.stat(self.key_path).st_mode), 0o600)

    def test_recovers_missing_public_key(self):
        pub = ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        pub.unlink()
        pub_again = ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        self.assertTrue(pub_again.is_file())
        self.assertTrue(pub_again.read_text(encoding="utf-8").startswith("ssh-ed25519 "))

    def test_missing_ssh_keygen_raises_runtime_error(self):
        with mock.patch("agent_sandbox.ssh.shutil.which", return_value=None):
            with self.assertRaises(RuntimeError) as ctx:
                ensure_ssh_keypair(self.manifest, self.sandbox_dir)
        self.assertIn("ssh-keygen", str(ctx.exception))

    def test_non_ssh_connection_rejected(self):
        web = EngineManifest(
            name="webby", port=1, connection=EngineConnectionConfig(type="web", url="http://127.0.0.1:1")
        )
        with self.assertRaises(ValueError):
            ensure_ssh_keypair(web, self.sandbox_dir)


class TestEnsureSshClientConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_ssh_config_")
        self.sandbox_dir = os.path.join(self.tmpdir, "sandbox")
        self.fs_root = Path(self.tmpdir) / "home"
        self.fs_root.mkdir(parents=True, exist_ok=True)
        self.manifest = _ssh_manifest()
        self.engine_ssh_dir = Path(self.sandbox_dir) / "fake" / "ssh"
        self.user_ssh_dir = self.fs_root / ".ssh"
        self.user_ssh_config = self.user_ssh_dir / "config"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _expected_block(self, manifest: EngineManifest | None = None) -> str:
        manifest = manifest or self.manifest
        name = manifest.name
        alias = f"agent-sandbox-{name}"
        ssh_dir = Path(self.sandbox_dir) / name / "ssh"
        return (
            f"# >>> agent-sandbox:{name} >>>\n"
            f"Host {alias}\n"
            "    HostName 127.0.0.1\n"
            f"    Port {manifest.port}\n"
            "    User developer\n"
            f"    IdentityFile {ssh_dir / 'id_ed25519'}\n"
            "    IdentitiesOnly yes\n"
            "    StrictHostKeyChecking accept-new\n"
            f"    HostKeyAlias {alias}\n"
            f"    UserKnownHostsFile {ssh_dir / 'known_hosts'}\n"
            f"# <<< agent-sandbox:{name} <<<"
        )

    def test_block_content_and_returned_path(self):
        config_path = ensure_ssh_client_config(
            self.manifest, self.sandbox_dir, fs_root=self.fs_root
        )
        self.assertEqual(config_path, self.user_ssh_config)
        self.assertEqual(
            self.user_ssh_config.read_text(encoding="utf-8"),
            self._expected_block() + "\n",
        )
        # The IdentityFile it references must exist.
        key_path = self.engine_ssh_dir / "id_ed25519"
        self.assertTrue(key_path.is_file())
        self.assertEqual(stat.S_IMODE(os.stat(key_path).st_mode), 0o600)

    def test_block_prepended_before_preexisting_host_star(self):
        # OpenSSH is first-match-wins: our Host entry must precede any user
        # `Host *` catch-all, and app host pickers scan ~/.ssh/config directly.
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        self.user_ssh_config.write_text(
            "Host *\n    ForwardAgent yes\n    UserKnownHostsFile /dev/null\n",
            encoding="utf-8",
        )

        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)

        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# >>> agent-sandbox:fake >>>\n"))
        self.assertLess(content.index("Host agent-sandbox-fake\n"), content.index("Host *"))
        self.assertIn("Host *\n    ForwardAgent yes", content)
        # Exactly one blank line separates our block from the preserved rest.
        self.assertIn("# <<< agent-sandbox:fake <<<\n\nHost *\n", content)

    def test_idempotent_and_byte_stable_when_called_twice(self):
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        first = self.user_ssh_config.read_bytes()
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        second = self.user_ssh_config.read_bytes()

        self.assertEqual(first, second)
        content = second.decode("utf-8")
        self.assertEqual(content.count("# >>> agent-sandbox:fake >>>"), 1)
        self.assertEqual(content.count("# <<< agent-sandbox:fake <<<"), 1)
        self.assertEqual(content.count("Host agent-sandbox-fake\n"), 1)

    def test_reensure_updates_port_in_place(self):
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        ensure_ssh_client_config(
            _ssh_manifest(port=4321), self.sandbox_dir, fs_root=self.fs_root
        )

        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertEqual(content.count("# >>> agent-sandbox:fake >>>"), 1)
        self.assertIn("    Port 4321\n", content)
        self.assertNotIn("Port 1234", content)

    def test_preserves_unrelated_content_and_other_engine_blocks(self):
        other_block = (
            "# >>> agent-sandbox:other >>>\n"
            "Host agent-sandbox-other\n"
            "    Port 9999\n"
            "# <<< agent-sandbox:other <<<\n"
        )
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        self.user_ssh_config.write_text(
            "# my comment\n" + "Host *\n    ForwardAgent yes\n\n" + other_block,
            encoding="utf-8",
        )

        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)

        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertTrue(content.startswith("# >>> agent-sandbox:fake >>>\n"))
        self.assertLess(content.index("# <<< agent-sandbox:fake <<<"), content.index("# my comment"))
        self.assertIn("# my comment\nHost *\n    ForwardAgent yes\n", content)
        self.assertEqual(content.count("# >>> agent-sandbox:other >>>"), 1)
        self.assertIn("Host agent-sandbox-other", content)
        self.assertLess(content.index("Host *"), content.index("Host agent-sandbox-other"))

    def test_orphaned_begin_marker_dropped_to_eof(self):
        # A malformed block (begin marker without an end marker) is replaced:
        # the orphan and everything after it are dropped, then ours is prepended.
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        self.user_ssh_config.write_text(
            "# >>> agent-sandbox:fake >>>\nHost stale\n    Port 1\n",
            encoding="utf-8",
        )

        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)

        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertEqual(content, self._expected_block() + "\n")
        self.assertEqual(content.count("# >>> agent-sandbox:fake >>>"), 1)

    def test_user_ssh_dir_and_config_permissions(self):
        # An existing but loosely-permissioned ~/.ssh is tightened to 0700.
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.user_ssh_dir, 0o755)

        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)

        self.assertEqual(stat.S_IMODE(os.stat(self.user_ssh_dir).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.user_ssh_config).st_mode), 0o600)

        # A no-op re-run still ends with a 0600 config.
        os.chmod(self.user_ssh_config, 0o644)
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        self.assertEqual(stat.S_IMODE(os.stat(self.user_ssh_config).st_mode), 0o600)

    def test_multiple_engines_coexist_without_clobbering(self):
        second = _ssh_manifest(name="second", port=5678)
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        ensure_ssh_client_config(second, self.sandbox_dir, fs_root=self.fs_root)

        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertEqual(content.count("# >>> agent-sandbox:fake >>>"), 1)
        self.assertEqual(content.count("# >>> agent-sandbox:second >>>"), 1)
        self.assertIn("Host agent-sandbox-fake", content)
        self.assertIn("Host agent-sandbox-second", content)
        self.assertIn("    Port 5678\n", content)

        # Re-running the first engine must not drop or duplicate the second.
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertEqual(content.count("# >>> agent-sandbox:fake >>>"), 1)
        self.assertEqual(content.count("# <<< agent-sandbox:second <<<"), 1)
        self.assertIn("Host agent-sandbox-second", content)


class TestRemoveSshClientConfig(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_ssh_remove_")
        self.sandbox_dir = os.path.join(self.tmpdir, "sandbox")
        self.fs_root = Path(self.tmpdir) / "home"
        self.fs_root.mkdir(parents=True, exist_ok=True)
        self.manifest = _ssh_manifest()
        self.user_ssh_dir = self.fs_root / ".ssh"
        self.user_ssh_config = self.user_ssh_dir / "config"

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_removes_only_this_engine_block_and_reports_change(self):
        original = "Host *\n    ForwardAgent yes\n"
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        self.user_ssh_config.write_text(original, encoding="utf-8")
        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        other_block = (
            "# >>> agent-sandbox:other >>>\n"
            "Host agent-sandbox-other\n"
            "# <<< agent-sandbox:other <<<\n"
        )
        with self.user_ssh_config.open("a", encoding="utf-8") as fh:
            fh.write(other_block)

        changed = remove_ssh_client_config(
            self.manifest, self.sandbox_dir, fs_root=self.fs_root
        )
        self.assertTrue(changed)

        content = self.user_ssh_config.read_text(encoding="utf-8")
        self.assertNotIn("agent-sandbox:fake", content)
        self.assertNotIn("Host agent-sandbox-fake", content)
        self.assertIn("Host *\n    ForwardAgent yes\n", content)
        self.assertEqual(content.count("# >>> agent-sandbox:other >>>"), 1)
        self.assertIn("Host agent-sandbox-other", content)
        self.assertEqual(stat.S_IMODE(os.stat(self.user_ssh_config).st_mode), 0o600)

        # Removing again is a no-op and reports no change.
        os.chmod(self.user_ssh_config, 0o644)
        changed_again = remove_ssh_client_config(
            self.manifest, self.sandbox_dir, fs_root=self.fs_root
        )
        self.assertFalse(changed_again)
        self.assertEqual(self.user_ssh_config.read_text(encoding="utf-8"), content)
        self.assertEqual(stat.S_IMODE(os.stat(self.user_ssh_config).st_mode), 0o600)

    def test_ensure_then_remove_restores_previous_content(self):
        original = "# my comment\nHost *\n    ForwardAgent yes\n"
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        self.user_ssh_config.write_text(original, encoding="utf-8")

        ensure_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        self.assertTrue(
            remove_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        )
        self.assertEqual(self.user_ssh_config.read_text(encoding="utf-8"), original)

    def test_remove_missing_file_returns_false(self):
        self.assertFalse(
            remove_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        )
        self.assertFalse(self.user_ssh_config.exists())

    def test_remove_orphaned_begin_marker_drops_to_eof(self):
        self.user_ssh_dir.mkdir(parents=True, exist_ok=True)
        self.user_ssh_config.write_text(
            "# >>> agent-sandbox:fake >>>\nHost stale\n    Port 1\n",
            encoding="utf-8",
        )
        self.assertTrue(
            remove_ssh_client_config(self.manifest, self.sandbox_dir, fs_root=self.fs_root)
        )
        self.assertEqual(self.user_ssh_config.read_text(encoding="utf-8"), "")


if __name__ == "__main__":
    unittest.main()
