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

"""The stamper loop must survive pending-fill read errors.

The per-iteration reads that feed pending_commitments — journal[idx]
(guarded only for KeyError, the normal end-of-journal exit) and the
calendar LevelDB membership check — sit outside the __do_bitcoin try. An
OSError from either killed the anchoring thread while the HTTP server
kept accepting digests: the same silent-death class as the ValueError
regression in test_stamper_loop.py, but via the read path.

Survival must err low: a failed read adds no pending commitments and
invents nothing; anchoring of what is already pending continues, the
failure is warned once (not once per second), and the fill retries.
"""

import os
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from otsserver.calendar import Journal
from otsserver.stamper import Stamper


class Test_stamper_survives_read_errors(unittest.TestCase):
    def test_journal_oserror_warns_once_and_stamping_continues(self):
        with tempfile.TemporaryDirectory() as cal_path:
            open(os.path.join(cal_path, 'journal'), 'wb').close()

            calendar = types.SimpleNamespace(path=cal_path)
            exit_event = threading.Event()

            with mock.patch.object(Journal, '__getitem__',
                                   side_effect=OSError("disk says no")):
                with mock.patch.object(Stamper, '_Stamper__do_bitcoin') \
                        as do_bitcoin:
                    try:
                        with self.assertLogs(level='WARNING') as captured:
                            stamper = Stamper(calendar, exit_event,
                                              conf_target=12,
                                              relay_feerate=1,
                                              min_confirmations=6,
                                              min_tx_interval=0,
                                              max_fee=1000000,
                                              max_pending=100)
                            # __loop waits 1s between iterations; give it
                            # time to hit the poisoned read at least twice.
                            time.sleep(2.5)
                            alive = stamper.thread.is_alive()
                    finally:
                        exit_event.set()
                        stamper.thread.join(5)

            self.assertTrue(
                alive, "stamper thread died on a pending-fill read error")
            self.assertGreaterEqual(
                do_bitcoin.call_count, 2,
                "anchoring stopped: __do_bitcoin no longer reached")
            read_warnings = [line for line in captured.output
                             if "disk says no" in line]
            self.assertEqual(
                len(read_warnings), 1,
                "expected exactly one warning (warn once, not once per "
                "iteration): %r" % captured.output)


if __name__ == "__main__":
    unittest.main()
