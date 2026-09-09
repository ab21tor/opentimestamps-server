# Copyright (C) 2026 ab21tor
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

"""Regression tests for the otsd launcher's ~/.otsd handling.

The launcher unconditionally created ~/.otsd even when --calendar pointed
elsewhere — a stray dotdir in whatever $HOME the daemon ran under. The mkdir must happen only when the
default calendar path is actually in use.

Driven via subprocess: otsd is a script, and both runs exit early at
calendar-identity loading (donation_addr missing) — which is AFTER the
mkdir — so the dotdir's presence is fully decided by exit time either way.
"""

import os
import subprocess
import sys
import tempfile
import unittest

OTSD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    'otsd')


class Test_otsd_default_dir(unittest.TestCase):
    def _run(self, home, args):
        env = dict(os.environ, HOME=home)
        return subprocess.run([sys.executable, OTSD] + args,
                              env=env, capture_output=True, timeout=60)

    def test_custom_calendar_leaves_home_untouched(self):
        with tempfile.TemporaryDirectory() as home, \
             tempfile.TemporaryDirectory() as cal:
            result = self._run(home, ['--calendar', cal])
            self.assertNotEqual(result.returncode, 0)  # exits at missing identity
            self.assertFalse(
                os.path.exists(os.path.join(home, '.otsd')),
                "custom --calendar must not create ~/.otsd")

    def test_default_calendar_creates_default_dir(self):
        """Control: with the default path the dotdir IS created, proving the
        conditional (and this test's observation point) actually runs."""
        with tempfile.TemporaryDirectory() as home:
            result = self._run(home, [])
            self.assertNotEqual(result.returncode, 0)  # exits at missing identity
            self.assertTrue(
                os.path.isdir(os.path.join(home, '.otsd')),
                "default calendar path must create ~/.otsd")


if __name__ == "__main__":
    unittest.main()
