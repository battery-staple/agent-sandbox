"""Tests for volume migration in agent_sandbox.compose.volumes."""
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

PKG_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if PKG_SRC not in sys.path:
    sys.path.insert(0, PKG_SRC)

from agent_sandbox.compose.volumes import check_volume_exists, migrate_antigravity_volume_if_needed


class TestVolumes(unittest.TestCase):
    @patch("subprocess.run")
    def test_check_volume_exists(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="my_volume\nother_volume\n")
        self.assertTrue(check_volume_exists("my_volume"))
        self.assertFalse(check_volume_exists("nonexistent"))

    @patch("agent_sandbox.compose.volumes.check_volume_exists")
    @patch("subprocess.run")
    def test_migration_not_needed_if_legacy_missing(self, mock_run, mock_exists):
        mock_exists.return_value = False
        self.assertFalse(migrate_antigravity_volume_if_needed())
        mock_run.assert_not_called()

    @patch("agent_sandbox.compose.volumes.check_volume_exists")
    @patch("subprocess.run")
    def test_migration_not_needed_if_new_already_exists(self, mock_run, mock_exists):
        # legacy exists, new also exists
        mock_exists.side_effect = lambda vol: True
        self.assertFalse(migrate_antigravity_volume_if_needed())
        mock_run.assert_not_called()

    @patch("agent_sandbox.compose.volumes.check_volume_exists")
    @patch("subprocess.run")
    def test_migration_triggers_when_needed(self, mock_run, mock_exists):
        # legacy exists (True), new does not exist (False)
        mock_exists.side_effect = lambda vol: vol == "antigravity_home_persist"
        mock_run.return_value = MagicMock(returncode=0, stderr="")

        result = migrate_antigravity_volume_if_needed()
        self.assertTrue(result)
        self.assertEqual(mock_run.call_count, 2)  # 1 create, 1 copy


if __name__ == "__main__":
    unittest.main()
