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

"""An empty anchor wallet must not spam one ERROR per second.

__do_bitcoin logs "Can't timestamp; no spendable outputs" and returns; the
loop calls it every second, so a drained wallet wrote an ERROR line per
second until refilled. The fix is the warn-once-with-recovery pattern used
elsewhere in the fork: one ERROR on entering the empty state, one INFO
when spendable outputs return.
"""

import os
import tempfile
import threading
import types
import unittest
from unittest import mock

from otsserver.stamper import Stamper


class Test_empty_wallet_logs_once(unittest.TestCase):
    def make_stamper(self, cal_path):
        open(os.path.join(cal_path, 'journal'), 'wb').close()
        calendar = types.SimpleNamespace(path=cal_path)
        exit_event = threading.Event()
        exit_event.set()  # loop thread exits immediately; we drive directly
        stamper = Stamper(calendar, exit_event,
                          conf_target=12, relay_feerate=1,
                          min_confirmations=6, min_tx_interval=0,
                          max_fee=1000000, max_pending=100)
        stamper.thread.join(5)
        # Drive __do_bitcoin straight to the wallet check: no new blocks,
        # nothing unconfirmed, departure clock expired, one pending
        # commitment.
        stamper.known_blocks = mock.Mock(
            update_from_proxy=mock.Mock(return_value=[]))
        stamper.next_timestamp_tx = 0
        stamper.pending_commitments.add(b'x' * 44)
        return stamper

    def test_empty_wallet_one_error_then_recovery_info(self):
        with tempfile.TemporaryDirectory() as cal_path:
            stamper = self.make_stamper(cal_path)
            do_bitcoin = stamper._Stamper__do_bitcoin

            with mock.patch('otsserver.stamper.make_proxy',
                            return_value=mock.MagicMock()):
                with mock.patch('otsserver.stamper.find_unspent',
                                return_value=[]):
                    with self.assertLogs(level='ERROR') as captured:
                        do_bitcoin()
                        do_bitcoin()
                errors = [line for line in captured.output
                          if 'no spendable outputs' in line]
                self.assertEqual(
                    len(errors), 1,
                    "expected exactly one ERROR across two empty-wallet "
                    "passes (log once, not once per second): %r"
                    % captured.output)

                # Recovery: outputs reappear — one INFO says anchoring
                # resumes. Downstream tx construction runs into the mocks
                # and may raise; the INFO must already be out.
                with mock.patch('otsserver.stamper.find_unspent',
                                return_value=[{'outpoint': None, 'amount': 1}]):
                    with self.assertLogs(level='INFO') as recovered:
                        try:
                            do_bitcoin()
                        except Exception:
                            pass
                self.assertTrue(
                    any('spendable' in line.lower() and 'again' in line.lower()
                        for line in recovered.output),
                    recovered.output)


if __name__ == "__main__":
    unittest.main()
