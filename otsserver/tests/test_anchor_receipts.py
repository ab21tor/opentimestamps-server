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

"""Tests for anchor receipts (OTSD_ANCHOR_RECEIPTS).

The stamper appends exactly one JSONL line per anchor transaction at the
moment its existing confirmation logic saves the tx to the calendar
(__save_confirmed_timestamp_tx). The line format is an interface parsed by
the gateway's anchor billing, so its field set is pinned by these tests.

The tests drive the stamper's own data structures — UnconfirmedTimestampTx
entries standing in for a broadcast plus RBF bumps, a TimestampTx for the
mined tx — and call the real confirmation-save path. No bitcoind, no
network, matching the other branch-delta test modules.
"""

import json
import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from bitcoin.core import CTransaction, CTxIn, CTxOut, COutPoint, b2lx
from bitcoin.core.script import CScript, OP_RETURN
from opentimestamps.core.timestamp import Timestamp

from otsserver.stamper import Stamper, TimestampTx, UnconfirmedTimestampTx


def make_tx(seed, value=10000):
    """A minimal distinct transaction; only its txid matters here"""
    return CTransaction(
        [CTxIn(COutPoint(bytes([seed]) * 32, 0), nSequence=0xfffffffe)],
        [CTxOut(value, CScript([OP_RETURN, bytes([seed]) * 32]))])


def make_commitments(n):
    return [Timestamp(bytes([i]) * 32) for i in range(n)]


def make_stamper(receipts_path):
    """A Stamper with just the state the confirmation-save path touches

    Stamper.__init__ starts the stamping thread, so skip it and set the
    fields directly, as test_rpc_homepage does with SimpleNamespace.
    """
    stamper = Stamper.__new__(Stamper)
    stamper.calendar = mock.Mock()
    stamper.anchor_receipts_path = receipts_path
    stamper.min_confirmations = 6
    stamper.unconfirmed_txs = []
    stamper.txs_waiting_for_confirmation = {}
    return stamper


def save(stamper, mined_tx):
    stamper._Stamper__save_confirmed_timestamp_tx(mined_tx)


def simulate_rbf_cycle(stamper):
    """Broadcast + two RBF bumps, then the final version mined at 850000

    Returns the mined TimestampTx exactly as __do_bitcoin would build it:
    the last sent version, carrying its own fee, with every prior version
    erased from unconfirmed_txs.
    """
    tip = Timestamp(b'\xff' * 32)
    commitments = make_commitments(3)

    replaced_a, replaced_b, final = make_tx(1), make_tx(2), make_tx(3)
    for tx, fee in ((replaced_a, 100), (replaced_b, 150), (final, 200)):
        stamper.unconfirmed_txs.append(
            UnconfirmedTimestampTx(tx, tip, len(commitments), fee))

    mined_tx = TimestampTx(final, tip, commitments,
                           stamper.unconfirmed_txs[-1].fee, 850000)
    stamper.unconfirmed_txs.clear()
    stamper.txs_waiting_for_confirmation[850000] = mined_tx
    return (replaced_a, replaced_b, final), mined_tx


class Test_anchor_receipts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.receipts_path = os.path.join(self.tmpdir.name,
                                          'anchor-receipts.jsonl')

    def read_lines(self):
        with open(self.receipts_path, 'rb') as fd:
            return fd.read().decode().splitlines()

    def test_rbf_sequence_writes_one_line_with_final_fee(self):
        stamper = make_stamper(self.receipts_path)
        (replaced_a, replaced_b, final), _ = simulate_rbf_cycle(stamper)

        confirmed_tx = stamper.txs_waiting_for_confirmation.pop(850000)
        save(stamper, confirmed_tx)

        lines = self.read_lines()
        self.assertEqual(len(lines), 1)
        receipt = json.loads(lines[0])
        self.assertEqual(receipt['txid'], b2lx(final.GetTxid()))
        self.assertEqual(receipt['fee_sats'], 200)
        for replaced in (replaced_a, replaced_b):
            self.assertNotIn(b2lx(replaced.GetTxid()), lines[0])

    def test_broadcasts_and_bumps_alone_write_nothing(self):
        stamper = make_stamper(self.receipts_path)
        simulate_rbf_cycle(stamper)

        # Broadcast, two bumps, and even the mined-but-unconfirmed tx: no
        # receipt until the stamper's own logic deems the tx confirmed.
        self.assertFalse(os.path.exists(self.receipts_path))

    def test_unset_env_writes_nothing_and_saves_calendar_unchanged(self):
        stamper = make_stamper(None)
        _, mined_tx = simulate_rbf_cycle(stamper)

        save(stamper, stamper.txs_waiting_for_confirmation.pop(850000))

        stamper.calendar.add_commitment_timestamps.assert_called_once_with(
            mined_tx.commitment_timestamps)
        self.assertEqual(os.listdir(self.tmpdir.name), [])

    def test_init_reads_env(self):
        def init_stamper():
            with mock.patch.object(Stamper, '_Stamper__loop'):
                stamper = Stamper(mock.Mock(), threading.Event(),
                                  conf_target=12,
                                  relay_feerate=1,
                                  min_confirmations=6,
                                  min_tx_interval=0,
                                  max_fee=1000000,
                                  max_pending=100)
                stamper.thread.join(5)
                return stamper

        with mock.patch.dict('os.environ'):
            os.environ.pop('OTSD_ANCHOR_RECEIPTS', None)
            self.assertIsNone(init_stamper().anchor_receipts_path)

        with mock.patch.dict('os.environ',
                             {'OTSD_ANCHOR_RECEIPTS': self.receipts_path}):
            self.assertEqual(init_stamper().anchor_receipts_path,
                             self.receipts_path)

    def test_write_failure_does_not_break_confirmation(self):
        bad_path = os.path.join(self.tmpdir.name, 'no-such-dir', 'r.jsonl')
        stamper = make_stamper(bad_path)
        (_, _, final), mined_tx = simulate_rbf_cycle(stamper)

        with self.assertLogs(level='WARNING') as captured:
            save(stamper, stamper.txs_waiting_for_confirmation.pop(850000))

        stamper.calendar.add_commitment_timestamps.assert_called_once_with(
            mined_tx.commitment_timestamps)
        # Two writes fail on an unwritable path — the pending marker before
        # the save and the receipt after it — and each warns, naming the tx.
        warnings = [r for r in captured.records
                    if r.levelname == 'WARNING'
                    and 'anchor receipt' in r.getMessage()]
        self.assertEqual(len(warnings), 2, captured.output)
        for w in warnings:
            self.assertIn(b2lx(final.GetTxid()), w.getMessage())

    def test_append_only_existing_bytes_untouched(self):
        stamper = make_stamper(self.receipts_path)
        simulate_rbf_cycle(stamper)
        save(stamper, stamper.txs_waiting_for_confirmation.pop(850000))

        with open(self.receipts_path, 'rb') as fd:
            first_write = fd.read()

        second_tx = make_tx(9)
        save(stamper, TimestampTx(second_tx, Timestamp(b'\xee' * 32),
                                  make_commitments(2), 321, 850007))

        with open(self.receipts_path, 'rb') as fd:
            both_writes = fd.read()
        self.assertTrue(both_writes.startswith(first_write))

        lines = self.read_lines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[1])['txid'], b2lx(second_tx.GetTxid()))

    def test_pinned_format_fields(self):
        stamper = make_stamper(self.receipts_path)
        (_, _, final), mined_tx = simulate_rbf_cycle(stamper)

        before = int(time.time())
        save(stamper, stamper.txs_waiting_for_confirmation.pop(850000))
        after = int(time.time())

        receipt = json.loads(self.read_lines()[0])
        self.assertEqual(set(receipt.keys()),
                         {'txid', 'fee_sats', 'commitments',
                          'confirmed_height', 'confirmed_at', 'records'})

        self.assertIsInstance(receipt['txid'], str)
        self.assertEqual(len(receipt['txid']), 64)
        bytes.fromhex(receipt['txid'])
        self.assertEqual(receipt['txid'], b2lx(final.GetTxid()))

        self.assertIsInstance(receipt['fee_sats'], int)
        self.assertEqual(receipt['fee_sats'], 200)

        self.assertIsInstance(receipt['commitments'], int)
        self.assertEqual(receipt['commitments'],
                         len(mined_tx.commitment_timestamps))

        self.assertIsInstance(receipt['confirmed_height'], int)
        self.assertEqual(receipt['confirmed_height'], 850000)

        self.assertIsInstance(receipt['confirmed_at'], int)
        self.assertTrue(before <= receipt['confirmed_at'] <= after)


if __name__ == "__main__":
    unittest.main()
