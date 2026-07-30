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

"""Tests for the free-running anchor departure clock.

Upstream only ever arms next_timestamp_tx when a transaction confirms
(__do_bitcoin's post-confirmation reschedule). When that moment passes over
an empty pending queue the timer just stays expired, so the first commitment
to arrive after an idle stretch triggered a broadcast within seconds --
timestamping the customer's arrival onto the public chain, and (since
successive anchors spend the same wallet's change) making the box's entire
timing history coin-graph-linkable from one identified anchor. Upstream
never meets this path: public calendars are never idle. On a single-operator
calendar it is the common case, so this fork rolls the clock forward over an
empty queue and arms it at startup. Departures happen only at scheduled
moments of a free-running jittered clock (min_tx_interval x uniform(1, 2));
broadcast times are a property of the box's own schedule, independent of
submission times.

Same harness as the sibling branch-delta modules: Stamper.__new__ plus
direct field setup, make_proxy/find_unspent patched at module level. No
bitcoind, no network.
"""

import threading
import time
import unittest
from unittest import mock

from bitcoin.core import CTransaction, CTxIn, CTxOut, COutPoint
from bitcoin.core.script import CScript, OP_RETURN

from otsserver.stamper import OrderedSet, Stamper, UnconfirmedTimestampTx

INTERVAL = 600


def make_stamper():
    """A Stamper with just the state __do_bitcoin's departure gate touches

    Stamper.__init__ starts the stamping thread, so skip it and set the
    fields directly, as the sibling modules do.
    """
    stamper = Stamper.__new__(Stamper)
    stamper.calendar = mock.Mock()
    stamper.anchor_receipts_path = None
    stamper.conf_target = 12
    stamper.relay_feerate = 1
    stamper.min_confirmations = 6
    stamper.min_tx_interval = INTERVAL
    stamper.max_fee = 1000000
    stamper.max_pending = 100
    stamper.known_blocks = mock.Mock()
    stamper.known_blocks.update_from_proxy.return_value = []
    stamper.unconfirmed_txs = []
    stamper.pending_commitments = OrderedSet()
    stamper.txs_waiting_for_confirmation = {}
    stamper.next_timestamp_tx = 0
    return stamper


def do_bitcoin(stamper):
    stamper._Stamper__do_bitcoin()


class Test_departure_clock(unittest.TestCase):
    def test_expired_timer_over_empty_queue_rolls_clock(self):
        stamper = make_stamper()
        stamper.next_timestamp_tx = time.time() - 3600

        with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
                mock.patch('otsserver.stamper.find_unspent') as find_unspent:
            before = time.time()
            do_bitcoin(stamper)
            after = time.time()

        self.assertGreaterEqual(stamper.next_timestamp_tx, before + INTERVAL)
        self.assertLessEqual(stamper.next_timestamp_tx, after + 2 * INTERVAL)
        find_unspent.assert_not_called()
        make_proxy.return_value._call.assert_not_called()
        make_proxy.return_value.sendrawtransaction.assert_not_called()

    def test_commitment_after_idleness_waits_for_schedule(self):
        stamper = make_stamper()
        stamper.next_timestamp_tx = time.time() - 3600

        with mock.patch('otsserver.stamper.make_proxy'), \
                mock.patch('otsserver.stamper.find_unspent',
                           return_value=[]) as find_unspent:
            # Idle pass rolls the clock into the future.
            do_bitcoin(stamper)
            armed = stamper.next_timestamp_tx
            self.assertGreater(armed, time.time())

            # A commitment arriving after idleness must NOT cause a send:
            # the next pass returns at the waiting branch, clock untouched.
            stamper.pending_commitments.add(b'\x01' * 32)
            do_bitcoin(stamper)
            find_unspent.assert_not_called()
            self.assertEqual(stamper.next_timestamp_tx, armed)

            # Only once the schedule itself allows is a departure permitted:
            # find_unspent is reached (with no spendable outputs the attempt
            # aborts benignly). The attempt does not re-roll the clock --
            # rescheduling on the active path remains tied to confirmations.
            stamper.next_timestamp_tx = time.time() - 1
            scheduled = stamper.next_timestamp_tx
            with self.assertLogs(level='ERROR') as captured:
                do_bitcoin(stamper)
            find_unspent.assert_called_once()
            self.assertEqual(stamper.next_timestamp_tx, scheduled)
            self.assertTrue(any('no spendable outputs' in line
                                for line in captured.output), captured.output)

    def test_confirmation_path_schedules_as_before(self):
        stamper = make_stamper()
        stamper.pending_commitments.add(b'\x02' * 32)
        stamper.next_timestamp_tx = time.time() - 3600

        # A real transaction whose serialization embeds the merkle tip, as
        # __do_bitcoin's own broadcasts do; a single-tx block mock whose
        # merkle root is therefore that txid.
        tip_timestamp, _ = stamper._Stamper__pending_to_merkle_tree(1)
        tx = CTransaction(
            [CTxIn(COutPoint(b'\x03' * 32, 0), nSequence=0xfffffffd)],
            [CTxOut(0, CScript([OP_RETURN, tip_timestamp.msg]))])
        stamper.unconfirmed_txs.append(
            UnconfirmedTimestampTx(tx, tip_timestamp, 1, 100))

        block = mock.Mock(vtx=[tx], hashMerkleRoot=tx.GetTxid())
        stamper.known_blocks.update_from_proxy.return_value = \
            [(850000, b'\x04' * 32)]

        with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
                mock.patch('otsserver.stamper.find_unspent') as find_unspent:
            make_proxy.return_value.getblock.return_value = block
            before = time.time()
            do_bitcoin(stamper)
            after = time.time()

        # The post-confirmation reschedule law is exactly as before ...
        self.assertGreaterEqual(stamper.next_timestamp_tx, before + INTERVAL)
        self.assertLessEqual(stamper.next_timestamp_tx, after + 2 * INTERVAL)
        # ... and so is the confirmation bookkeeping.
        self.assertIn(850000, stamper.txs_waiting_for_confirmation)
        self.assertEqual(len(stamper.pending_commitments), 0)
        self.assertEqual(stamper.unconfirmed_txs, [])
        find_unspent.assert_not_called()

    def test_startup_arms_clock(self):
        with mock.patch.object(Stamper, '_Stamper__loop'):
            before = time.time()
            stamper = Stamper(mock.Mock(), threading.Event(),
                              conf_target=12,
                              relay_feerate=1,
                              min_confirmations=6,
                              min_tx_interval=INTERVAL,
                              max_fee=1000000,
                              max_pending=100)
            after = time.time()
            stamper.thread.join(5)

        self.assertGreaterEqual(stamper.next_timestamp_tx, before + INTERVAL)
        self.assertLessEqual(stamper.next_timestamp_tx, after + 2 * INTERVAL)


if __name__ == "__main__":
    unittest.main()
