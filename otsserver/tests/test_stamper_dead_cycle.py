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

"""A dead anchor cycle is abandoned loudly, never waited on forever.

An in-flight anchor is replaced by RBF bumps that spend the same input. If
that input stops being a confirmed unspent output -- a shallow reorg took
the parent whose change it is, or a version this stamper does not track
(one a restart forgot) was mined -- no bump can be priced: _get_tx_fee
returns None on every pass, and the pre-fix loop skipped at DEBUG level
until a restart, while the commitments sat pending. Now the stamper warns
once, drops the dead versions, and the next pass starts a fresh cycle from
the wallet's confirmed outputs. Billing is untouched: nothing leaves
pending, and the eventual anchor is receipted once.

Fails on the pre-fix code: unconfirmed_txs keeps the dead version and no
WARNING is logged.
"""

import time
import unittest
from unittest import mock

from opentimestamps.core.timestamp import Timestamp

from otsserver.stamper import UnconfirmedTimestampTx
from otsserver.tests.test_anchor_records import make_stamper, make_prev_tx, do_bitcoin


def stamper_with_dead_cycle():
    stamper = make_stamper(receipts_path=None)
    stamper.pending_commitments.add(b'\x01' * 44)
    stamper.pending_commitments.add(b'\x02' * 44)
    stamper.unconfirmed_txs.append(
        UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32), 2, 100))
    stamper.next_timestamp_tx = time.time() - 1
    return stamper


def new_empty_block(stamper, height):
    stamper.known_blocks.update_from_proxy.return_value = \
        [(height, bytes([height % 256]) * 32)]


class Test_dead_cycle(unittest.TestCase):
    def test_unpriceable_bump_abandons_the_cycle_with_one_warning(self):
        stamper = stamper_with_dead_cycle()
        new_empty_block(stamper, 100)
        with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
                mock.patch('otsserver.stamper._get_tx_fee', return_value=None):
            make_proxy.return_value.getblock.return_value = mock.Mock(vtx=[])
            make_proxy.return_value.getblockcount.return_value = 100
            with self.assertLogs(level='WARNING') as captured:
                do_bitcoin(stamper)

        warnings = [r for r in captured.records
                    if r.levelname == 'WARNING' and 'abandoned' in r.getMessage()]
        self.assertEqual(len(warnings), 1, captured.output)
        self.assertIn('2 commitments stay pending', warnings[0].getMessage())
        # The dead versions are gone; the commitments are not.
        self.assertEqual(stamper.unconfirmed_txs, [])
        self.assertEqual(len(stamper.pending_commitments), 2)

    def test_next_pass_starts_a_fresh_cycle_from_the_wallet(self):
        stamper = stamper_with_dead_cycle()
        new_empty_block(stamper, 100)
        with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
                mock.patch('otsserver.stamper._get_tx_fee', return_value=None):
            make_proxy.return_value.getblock.return_value = mock.Mock(vtx=[])
            make_proxy.return_value.getblockcount.return_value = 100
            with self.assertLogs(level='WARNING'):
                do_bitcoin(stamper)

        # No new block this pass: with the dead cycle gone the stamper no
        # longer waits for one, and consults the wallet for a fresh cycle.
        stamper.known_blocks.update_from_proxy.return_value = []
        with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
                mock.patch('otsserver.stamper.find_unspent', return_value=[]) as find_unspent:
            with self.assertLogs(level='ERROR'):   # empty wallet double: the fresh cycle ran into it
                do_bitcoin(stamper)
        find_unspent.assert_called_once()

    def test_first_transaction_of_a_cycle_keeps_the_quiet_skip(self):
        # No in-flight version: an unpriceable template is the old debug
        # skip, not an abandoned cycle.
        stamper = make_stamper(receipts_path=None)
        stamper.pending_commitments.add(b'\x01' * 44)
        stamper.next_timestamp_tx = time.time() - 1
        stamper.known_blocks.update_from_proxy.return_value = []
        unspent = [{'outpoint': make_prev_tx().vin[0].prevout, 'amount': 100000}]
        with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
                mock.patch('otsserver.stamper.find_unspent', return_value=unspent), \
                mock.patch('otsserver.stamper._get_tx_fee', return_value=None):
            proxy = make_proxy.return_value
            proxy._call.side_effect = lambda name, *a: (
                {'scriptPubKey': '0014' + '11' * 20} if name == 'getaddressinfo'
                else {'feerate': 0.00001} if name == 'estimatesmartfee' else 'bcrt1qaddress')
            proxy.signrawtransactionwithwallet.side_effect = lambda tx: {'complete': True, 'tx': tx}
            proxy.getblockcount.return_value = 100
            with self.assertNoLogs(level='WARNING'):
                do_bitcoin(stamper)
        self.assertEqual(stamper.unconfirmed_txs, [])


if __name__ == "__main__":
    unittest.main()
