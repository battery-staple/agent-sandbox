#!/usr/bin/env python3
"""
Regression tests for the AppleScript fallback approval dialog.

The primary Cocoa dialog passes user-controlled text as argv. The fallback
must do the same: interpolating arguments, cwd or notes into AppleScript
source allowed newline/quote breakouts. The script is now a constant and all
user text travels through argv.
"""

import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

from host_exec_daemon import FALLBACK_DIALOG_SCRIPT, prompt_user_approval_async

INJECTION_PAYLOADS = [
    'safe" & (do shell script "touch /tmp/pwned") & "',
    "safe\n& (do shell script \"touch /tmp/pwned\") & \"",
    "safe\r& (do shell script \"touch /tmp/pwned\") & \"",
    "back\\slash \"quote\"",
    ")* mutation *(",
]


class TestApprovalFallbackSafety(unittest.IsolatedAsyncioTestCase):
    def _mock_exec(self, fallback_stdout=b"Approve\n"):
        cocoa_fail = AsyncMock()
        cocoa_fail.communicate.return_value = (b"", b"cocoa unavailable\n")
        cocoa_fail.returncode = 1
        fallback = AsyncMock()
        fallback.communicate.return_value = (fallback_stdout, b"")
        fallback.returncode = 0
        return [cocoa_fail, fallback]

    async def test_fallback_passes_user_text_via_argv_not_script(self):
        for payload in INJECTION_PAYLOADS:
            with self.subTest(payload=payload):
                with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
                    mock_exec.side_effect = self._mock_exec()
                    approved = await prompt_user_approval_async(
                        "cmd",
                        [payload],
                        container_cwd="safe\n" + payload,
                    )

                self.assertTrue(approved)
                self.assertEqual(mock_exec.call_count, 2)
                fallback_args = mock_exec.call_args_list[1][0]

                # Shape: osascript -e <constant script> <prompt> <command display>
                self.assertEqual(fallback_args[0], "osascript")
                self.assertEqual(fallback_args[1], "-e")
                self.assertEqual(fallback_args[2], FALLBACK_DIALOG_SCRIPT)
                # No user data in the script; the script reads its input from argv.
                self.assertNotIn(payload, fallback_args[2])
                self.assertNotIn("do shell script", fallback_args[2])
                self.assertIn("item 1 of argv", fallback_args[2])
                # The payload is present only as inert argv data.
                self.assertIn(payload, fallback_args[4])
                self.assertIn(payload, fallback_args[3])

    async def test_fallback_script_is_constant_across_inputs(self):
        scripts = []
        for payload in ("a", INJECTION_PAYLOADS[0]):
            with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
                mock_exec.side_effect = self._mock_exec()
                await prompt_user_approval_async("cmd", [payload], cwd="/x")
            scripts.append(mock_exec.call_args_list[1][0][2])
        self.assertEqual(scripts[0], scripts[1])
        self.assertEqual(scripts[0], FALLBACK_DIALOG_SCRIPT)

    async def test_fallback_approve_and_deny_parsing(self):
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            mock_exec.side_effect = self._mock_exec(b"Approve\n")
            self.assertTrue(await prompt_user_approval_async("cmd", ["a"]))
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            mock_exec.side_effect = self._mock_exec(b"Deny\n")
            self.assertFalse(await prompt_user_approval_async("cmd", ["a"]))

    async def test_fallback_includes_cocoa_error_as_argv_data(self):
        with patch("asyncio.create_subprocess_exec", new=AsyncMock()) as mock_exec:
            mock_exec.side_effect = self._mock_exec()
            await prompt_user_approval_async("cmd", ["a"])
        prompt_arg = mock_exec.call_args_list[1][0][3]
        self.assertIn("Cocoa Alert Error", prompt_arg)


if __name__ == "__main__":
    unittest.main()
