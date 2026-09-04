# Copyright (C) 2026 The OpenTimestamps developers
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

"""A blocked fee cap must not log one ERROR per second.

When the next transaction would cost more than --btc-max-fee the stamper
declines to send it and tries again on the next pass; pre-fix that wrote
"Maximum txfee reached!" once per loop second for as long as feerates
stayed high. Same warn-once-with-recovery pattern as the empty wallet: one
ERROR on entering the blocked state, one INFO when a transaction goes out.

Fails on the pre-fix code: two blocked passes log two ERRORs.
"""

import time
import unittest
from unittest import mock

from opentimestamps.core.timestamp import Timestamp

from otsserver.stamper import UnconfirmedTimestampTx
from otsserver.tests.test_anchor_records import make_stamper, make_prev_tx, do_bitcoin


def blocked_stamper():
    stamper = make_stamper(receipts_path=None)
    stamper.max_fee = 1000
    stamper.pending_commitments.add(b'\x01' * 44)
    stamper.unconfirmed_txs.append(
        UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32), 1, 100))
    stamper.next_timestamp_tx = time.time() - 1
    return stamper


def pass_with_fee(stamper, height, fee):
    stamper.known_blocks.update_from_proxy.return_value = \
        [(height, bytes([height % 256]) * 32)]
    with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
            mock.patch('otsserver.stamper._get_tx_fee', return_value=fee):
        proxy = make_proxy.return_value
        proxy.getblock.return_value = mock.Mock(vtx=[])
        proxy.getblockcount.return_value = height
        proxy.signrawtransactionwithwallet.side_effect = lambda tx: {'complete': True, 'tx': tx}
        do_bitcoin(stamper)


class Test_fee_cap_logs_once(unittest.TestCase):
    def test_blocked_cap_one_error_then_recovery_info(self):
        stamper = blocked_stamper()
        with self.assertLogs(level='ERROR') as captured:
            pass_with_fee(stamper, 100, 5000)
            pass_with_fee(stamper, 101, 5000)
        errors = [line for line in captured.output if 'Maximum txfee' in line]
        self.assertEqual(len(errors), 1, captured.output)
        self.assertIn('fee 5000 > cap 1000', errors[0])
        self.assertEqual(len(stamper.unconfirmed_txs), 1)   # nothing sent while blocked

        with self.assertLogs(level='INFO') as recovered:
            pass_with_fee(stamper, 102, 500)
        self.assertTrue(any('under the cap' in line for line in recovered.output), recovered.output)
        self.assertEqual(len(stamper.unconfirmed_txs), 2)   # the bump went out

        # A second blocked stretch warns again -- once.
        with self.assertLogs(level='ERROR') as again:
            pass_with_fee(stamper, 103, 5000)
            pass_with_fee(stamper, 104, 5000)
        self.assertEqual(len([l for l in again.output if 'Maximum txfee' in l]), 1, again.output)


if __name__ == "__main__":
    unittest.main()
