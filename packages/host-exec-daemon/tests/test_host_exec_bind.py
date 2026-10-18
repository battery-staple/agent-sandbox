#!/usr/bin/env python3
"""
Regression tests for host-exec bind-address exposure reporting.

The bridge is authenticated only by its HMAC secret, so binding a wildcard
address exposes it to the network. The daemon cannot default to loopback
because the sandbox reaches it through the Docker gateway, so it must at least
report the exposure loudly at startup.
"""

import os
import sys
import unittest

PACKAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

from host_exec_daemon import describe_bind_exposure, warn_if_bind_is_exposed


class TestBindExposure(unittest.TestCase):
    def test_wildcard_addresses_are_reported(self):
        for address in ["0.0.0.0", "::", "*", "", "  0.0.0.0  ", "::0", "0:0:0:0:0:0:0:0"]:
            with self.subTest(address=address):
                message = describe_bind_exposure(address)
                self.assertIsNotNone(message)
                self.assertIn("wildcard", message)
                self.assertIn("HOST_EXEC_BIND=127.0.0.1", message)

    def test_specific_addresses_are_not_reported(self):
        for address in ["127.0.0.1", "::1", "localhost", "192.168.1.10"]:
            with self.subTest(address=address):
                self.assertIsNone(describe_bind_exposure(address))

    def test_warn_emits_a_log_for_wildcard(self):
        with self.assertLogs(level="WARNING") as cm:
            message = warn_if_bind_is_exposed("0.0.0.0")
        self.assertIsNotNone(message)
        self.assertIn("wildcard", "\n".join(cm.output))

    def test_warn_is_silent_for_loopback(self):
        with self.assertNoLogs(level="WARNING"):
            self.assertIsNone(warn_if_bind_is_exposed("127.0.0.1"))


if __name__ == "__main__":
    unittest.main()
