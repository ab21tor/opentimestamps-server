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

"""Tests for the "records" anchor-receipt field (OTSD_ANCHOR_RECEIPTS).

A record is one digest submission accepted by the aggregator: one leaf of a
per-second merkle tree, duplicates counted separately. Each per-second tree
becomes exactly one journal entry, so the aggregator persists the tree's
leaf count in a sidecar (journal.counts: one 4-byte big-endian integer per
journal entry index), written only after the journal entry itself is
durable. At each anchor-tree close the stamper sums the counts of exactly
the commitments in that tree; the sum rides beside fee through
UnconfirmedTimestampTx and TimestampTx into the receipt's "records" field.
Missing counts sum as 0 with one warning per tree: the number becomes a
bill, so miscounts must err low, never high.

True end-to-end aggregator-to-receipt is not feasible in this harness (the
span crosses the aggregator thread, the stamper thread, and a
broadcast/confirm cycle that exists only against bitcoind), so the two
halves are covered as units at the same level as the sibling modules:
aggregator/journal counting against a real Calendar in a tmpdir, and
close-time summing through the stamper's real __do_bitcoin paths with the
proxy mocked. No bitcoind, no network.
"""

import hashlib
import json
import os
import struct
import tempfile
import threading
import time
import unittest
from unittest import mock

from bitcoin.core import CTransaction, CTxIn, CTxOut, COutPoint, b2lx
from bitcoin.core.script import CScript, OP_RETURN
from opentimestamps.core.timestamp import Timestamp

from otsserver.calendar import Aggregator, Calendar, Journal, RecordCounts
from otsserver.stamper import OrderedSet, Stamper, UnconfirmedTimestampTx

INTERVAL = 600


def pack_count(n):
    return struct.pack('>L', n)


class Test_record_counts_sidecar(unittest.TestCase):
    """Aggregator/journal half: per-tree leaf counts become durable."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cal_path = os.path.join(self.tmpdir.name, 'calendar')
        os.makedirs(self.cal_path)
        with open(os.path.join(self.cal_path, 'uri'), 'w') as fd:
            fd.write('http://127.0.0.1:14788\n')
        with open(os.path.join(self.cal_path, 'hmac-key'), 'wb') as fd:
            fd.write(b'\x01' * 32)
        self.receipts_path = os.path.join(self.tmpdir.name, 'receipts.jsonl')
        self.journal_path = os.path.join(self.cal_path, 'journal')
        self.counts_path = self.journal_path + '.counts'

    def make_calendar(self, receipts_on):
        with mock.patch.dict(os.environ):
            os.environ.pop('OTSD_ANCHOR_RECEIPTS', None)
            if receipts_on:
                os.environ['OTSD_ANCHOR_RECEIPTS'] = self.receipts_path
            return Calendar(self.cal_path)

    def submit_two_trees(self, cal):
        """Two per-second trees, 3 and 2 records, in two distinct seconds"""
        with mock.patch('otsserver.calendar.time') as fake_time:
            fake_time.time.side_effect = [1000000000, 1000000001]
            cal.submit(Timestamp(hashlib.sha256(b'tree A').digest()),
                       records=3)
            cal.submit(Timestamp(hashlib.sha256(b'tree B').digest()),
                       records=2)

    def test_submit_persists_counts_per_tree_across_two_seconds(self):
        cal = self.make_calendar(receipts_on=True)
        self.submit_two_trees(cal)

        # Two journal entries, one per per-second tree, seconds distinct.
        journal = Journal(self.journal_path)
        self.assertEqual(journal[0][0:4], struct.pack('>L', 1000000000))
        self.assertEqual(journal[1][0:4], struct.pack('>L', 1000000001))
        with self.assertRaises(KeyError):
            journal[2]

        # The sidecar holds exactly the per-tree leaf counts, by entry index.
        with open(self.counts_path, 'rb') as fd:
            self.assertEqual(fd.read(), pack_count(3) + pack_count(2))

    def test_aggregator_passes_leaf_count_with_duplicates(self):
        # Duplicate digests are separate submissions, so separate records.
        cal = mock.Mock()
        exit_event = threading.Event()
        aggregator = Aggregator(cal, exit_event, commitment_interval=0.2)
        try:
            events = [threading.Event() for _ in range(3)]
            digests = [Timestamp(hashlib.sha256(b'dup').digest()),
                       Timestamp(hashlib.sha256(b'dup').digest()),
                       Timestamp(hashlib.sha256(b'other').digest())]
            for digest, event in zip(digests, events):
                aggregator.digest_queue.put((digest, event))
            for event in events:
                self.assertTrue(event.wait(5))
        finally:
            exit_event.set()
            aggregator.thread.join(5)

        # However the loop's wakeups split the queue into rounds, every
        # submission must be counted exactly once.
        self.assertGreaterEqual(cal.submit.call_count, 1)
        self.assertEqual(sum(c.kwargs['records']
                             for c in cal.submit.call_args_list), 3)

    def test_env_unset_writes_journal_but_no_sidecar(self):
        cal = self.make_calendar(receipts_on=False)
        self.submit_two_trees(cal)

        journal = Journal(self.journal_path)
        self.assertEqual(journal[0][0:4], struct.pack('>L', 1000000000))
        self.assertFalse(os.path.exists(self.counts_path))

    def test_sidecar_write_failure_never_loses_commitment(self):
        cal = self.make_calendar(receipts_on=True)

        with mock.patch('otsserver.calendar.RecordCountsWriter.put',
                        side_effect=OSError('disk says no')):
            with self.assertLogs(level='WARNING') as captured:
                with mock.patch('otsserver.calendar.time') as fake_time:
                    fake_time.time.side_effect = [1000000000]
                    cal.submit(
                        Timestamp(hashlib.sha256(b'tree A').digest()),
                        records=3)

        # The journal entry is durable; only the count was lost, and one
        # warning says so. Aggregation itself must never break.
        journal = Journal(self.journal_path)
        self.assertEqual(journal[0][0:4], struct.pack('>L', 1000000000))
        self.assertTrue(any('record count' in line
                            for line in captured.output), captured.output)

    def test_reader_treats_absent_zero_and_short_as_missing(self):
        counts = RecordCounts(self.counts_path)
        # Missing file: every count is unknown, and the reader must not
        # create the file (only the writer does, gated by the env var).
        self.assertIsNone(counts.get(0))
        self.assertFalse(os.path.exists(self.counts_path))

        # idx 0 valid; idx 1 an explicit zero (a hole reads identically);
        # idx 2 a torn 2-byte tail; idx 3 past EOF.
        with open(self.counts_path, 'wb') as fd:
            fd.write(pack_count(5) + pack_count(0) + b'\x00\x01')
        counts = RecordCounts(self.counts_path)
        self.assertEqual(counts.get(0), 5)
        self.assertIsNone(counts.get(1))
        self.assertIsNone(counts.get(2))
        self.assertIsNone(counts.get(3))

    def test_unreadable_sidecar_never_kills_the_scan(self):
        # get() runs in the stamper's journal-scan loop, outside the
        # __do_bitcoin try: a raise there kills the stamper thread and
        # anchoring stops silently while the process keeps serving.
        # Counting must never break stamping, so every read failure is
        # just an unknown count.

        # A directory at the sidecar path: open succeeds, every pread
        # fails with EISDIR.
        os.makedirs(self.counts_path)
        counts = RecordCounts(self.counts_path)

        with self.assertLogs(level='WARNING') as captured:
            self.assertIsNone(counts.get(0))
        warnings = [r for r in captured.records
                    if self.counts_path in r.getMessage()]
        self.assertEqual(len(warnings), 1, captured.output)

        # A permanently broken sidecar warns once, not per entry per scan.
        with self.assertNoLogs(level='WARNING'):
            self.assertIsNone(counts.get(1))
            self.assertIsNone(counts.get(2))

        # Repaired: the next successful read logs recovery once, then
        # counts flow silently again.
        os.rmdir(self.counts_path)
        with open(self.counts_path, 'wb') as fd:
            fd.write(pack_count(5))
        with self.assertLogs(level='INFO') as captured:
            self.assertEqual(counts.get(0), 5)
        self.assertTrue(any('readable again' in r.getMessage()
                            for r in captured.records), captured.output)
        with self.assertNoLogs():
            self.assertEqual(counts.get(0), 5)


def make_stamper(receipts_path):
    """A Stamper with just the state __do_bitcoin and the save path touch

    Stamper.__init__ starts the stamping thread, so skip it and set the
    fields directly, as the sibling modules do.
    """
    stamper = Stamper.__new__(Stamper)
    stamper.calendar = mock.Mock()
    stamper.anchor_receipts_path = receipts_path
    stamper.conf_target = 12
    stamper.relay_feerate = 1
    stamper.min_confirmations = 6
    stamper.min_tx_interval = INTERVAL
    stamper.max_fee = 1000000
    stamper.max_pending = 100
    stamper.known_blocks = mock.Mock()
    stamper.unconfirmed_txs = []
    stamper.pending_commitments = OrderedSet()
    stamper.txs_waiting_for_confirmation = {}
    stamper.commitment_records = {}
    stamper.commitment_idxs = {}
    stamper.journal_cursor = None
    stamper.next_timestamp_tx = 0
    return stamper


def make_prev_tx():
    """A signed-looking timestamp tx template: change output + OP_RETURN"""
    return CTransaction(
        [CTxIn(COutPoint(b'\x05' * 32, 0), nSequence=0xfffffffe)],
        [CTxOut(100000, CScript(bytes.fromhex('0014') + b'\x11' * 20)),
         CTxOut(0, CScript([OP_RETURN, b'\x00' * 32]))])


def do_bitcoin(stamper):
    stamper._Stamper__do_bitcoin()


def drive_broadcast(stamper, height, fee=555):
    """One __do_bitcoin pass ending in an RBF-replacement tree close

    A new (empty) block arrives so the pass gets past the no-new-blocks
    gate, the departure clock is expired, and the proxy is mocked just far
    enough for __update_timestamp_tx / sign / send to succeed. Returns the
    UnconfirmedTimestampTx the close appended.
    """
    stamper.known_blocks.update_from_proxy.return_value = \
        [(height, bytes([height % 256]) * 32)]
    stamper.next_timestamp_tx = time.time() - 1
    with mock.patch('otsserver.stamper.make_proxy') as make_proxy, \
            mock.patch('otsserver.stamper._get_tx_fee', return_value=fee):
        proxy = make_proxy.return_value
        proxy.getblock.return_value = mock.Mock(vtx=[])
        proxy.getblockcount.return_value = height
        proxy.signrawtransactionwithwallet.side_effect = \
            lambda tx: {'complete': True, 'tx': tx}
        do_bitcoin(stamper)
    return stamper.unconfirmed_txs[-1]


def drive_confirmation(stamper, height):
    """The latest unconfirmed tx is mined in a single-tx block at height"""
    sent_tx = stamper.unconfirmed_txs[-1].tx
    block = mock.Mock(vtx=[sent_tx], hashMerkleRoot=sent_tx.GetTxid())
    stamper.known_blocks.update_from_proxy.return_value = \
        [(height, b'\x06' * 32)]
    with mock.patch('otsserver.stamper.make_proxy') as make_proxy:
        make_proxy.return_value.getblock.return_value = block
        do_bitcoin(stamper)


def drive_depth(stamper, height):
    """An empty block at height; saves the tx that reaches min_confirmations"""
    stamper.known_blocks.update_from_proxy.return_value = \
        [(height, b'\x07' * 32)]
    with mock.patch('otsserver.stamper.make_proxy') as make_proxy:
        make_proxy.return_value.getblock.return_value = mock.Mock(vtx=[])
        do_bitcoin(stamper)


class Test_stamper_records(unittest.TestCase):
    """Stamper half: close-time summing, threading, and the receipt field."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.receipts_path = os.path.join(self.tmpdir.name,
                                          'anchor-receipts.jsonl')

    def read_receipts(self):
        with open(self.receipts_path, 'rb') as fd:
            return fd.read().decode().splitlines()

    def test_two_second_trees_sum_through_receipt(self):
        # Two per-second tree commitments: 3 records in one second, 2 in
        # the next (the counts test_record_counts_sidecar persists). The
        # receipt must carry records == 5 == N submissions.
        stamper = make_stamper(self.receipts_path)
        stamper.pending_commitments.add(b'\x01' * 44)
        stamper.pending_commitments.add(b'\x02' * 44)
        stamper.commitment_records = {b'\x01' * 44: 3, b'\x02' * 44: 2}
        stamper.unconfirmed_txs.append(
            UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32),
                                   0, 100))

        sent = drive_broadcast(stamper, 850000, fee=555)
        # The count is fixed at the close, and rides beside fee.
        self.assertEqual(sent.records, 5)
        self.assertEqual(sent.n, 2)
        self.assertEqual(sent.fee, 555)

        drive_confirmation(stamper, 850001)
        self.assertEqual(
            stamper.txs_waiting_for_confirmation[850001].records, 5)

        before = int(time.time())
        drive_depth(stamper, 850006)  # 850006 - 6 + 1 == 850001
        after = int(time.time())

        lines = self.read_receipts()
        self.assertEqual(len(lines), 1)
        receipt = json.loads(lines[0])
        self.assertEqual(receipt['records'], 5)
        # The five existing fields are untouched, values included.
        self.assertEqual(receipt['txid'], b2lx(sent.tx.GetTxid()))
        self.assertEqual(receipt['fee_sats'], 555)
        self.assertEqual(receipt['commitments'], 2)
        self.assertEqual(receipt['confirmed_height'], 850001)
        self.assertTrue(before <= receipt['confirmed_at'] <= after)

        # The anchor is final: its counts are released, never summed again.
        self.assertEqual(stamper.commitment_records, {})

    def test_missing_count_errs_low_warns_once_and_receipt_writes(self):
        stamper = make_stamper(self.receipts_path)
        for seed in (1, 2, 3):
            stamper.pending_commitments.add(bytes([seed]) * 44)
        # No count entry for the third commitment: it must sum as 0.
        stamper.commitment_records = {b'\x01' * 44: 4, b'\x02' * 44: 1}
        stamper.unconfirmed_txs.append(
            UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32),
                                   0, 100))

        with self.assertLogs(level='WARNING') as captured:
            sent = drive_broadcast(stamper, 850000)
        self.assertEqual(sent.records, 5)

        warnings = [r for r in captured.records
                    if r.levelname == 'WARNING'
                    and 'record count' in r.getMessage()]
        self.assertEqual(len(warnings), 1, captured.output)
        self.assertIn('1 of 3', warnings[0].getMessage())

        drive_confirmation(stamper, 850001)
        drive_depth(stamper, 850006)

        receipt = json.loads(self.read_receipts()[0])
        self.assertEqual(receipt['records'], 5)
        self.assertEqual(receipt['commitments'], 3)

    def test_each_close_carries_its_own_count(self):
        # An RBF replacement in this code re-closes over the then-current
        # pending: each close is its own tree with its own count, and the
        # receipt will carry the count of whichever tree confirms.
        stamper = make_stamper(self.receipts_path)
        stamper.pending_commitments.add(b'\x01' * 44)
        stamper.pending_commitments.add(b'\x02' * 44)
        stamper.commitment_records = {b'\x01' * 44: 3, b'\x02' * 44: 2}
        stamper.unconfirmed_txs.append(
            UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32),
                                   0, 100))

        first = drive_broadcast(stamper, 850000)
        self.assertEqual((first.n, first.records), (2, 5))

        # A third per-second tree arrives before the bump.
        stamper.pending_commitments.add(b'\x03' * 44)
        stamper.commitment_records[b'\x03' * 44] = 7

        second = drive_broadcast(stamper, 850001)
        self.assertEqual((second.n, second.records), (3, 12))
        # The first close's count is untouched by the re-close.
        self.assertEqual(stamper.unconfirmed_txs[-2].records, 5)

    def make_journal_files(self, cal_path):
        """A real journal of three entries; counts for idx 0 and 2 only"""
        entries = [struct.pack('>L', i) + bytes([0x10 + i]) * 40
                   for i in range(3)]
        with open(os.path.join(cal_path, 'journal'), 'wb') as fd:
            fd.write(b''.join(entries))
        with open(os.path.join(cal_path, 'journal') + '.counts', 'wb') as fd:
            fd.write(pack_count(3) + pack_count(0) + pack_count(2))
        return entries

    def run_scan(self, cal_path, receipts_on):
        """Run the real __loop once over the journal, __do_bitcoin mocked"""

        class FakeCalendar:
            path = cal_path

            def __contains__(self, commitment):
                return False

        exit_event = threading.Event()
        with mock.patch.dict(os.environ):
            os.environ.pop('OTSD_ANCHOR_RECEIPTS', None)
            if receipts_on:
                os.environ['OTSD_ANCHOR_RECEIPTS'] = self.receipts_path
            with mock.patch.object(Stamper, '_Stamper__do_bitcoin'):
                stamper = Stamper(FakeCalendar(), exit_event,
                                  conf_target=12,
                                  relay_feerate=1,
                                  min_confirmations=6,
                                  min_tx_interval=INTERVAL,
                                  max_fee=1000000,
                                  max_pending=100)
                deadline = time.time() + 5
                while (len(stamper.pending_commitments) < 3
                       and time.time() < deadline):
                    time.sleep(0.05)
                exit_event.set()
                stamper.thread.join(5)
        return stamper

    def test_scan_populates_counts_from_sidecar(self):
        entries = self.make_journal_files(self.tmpdir.name)
        stamper = self.run_scan(self.tmpdir.name, receipts_on=True)

        self.assertEqual(len(stamper.pending_commitments), 3)
        # idx 1 has a zero count: unknown, so absent — it will sum low.
        self.assertEqual(stamper.commitment_records,
                         {entries[0]: 3, entries[2]: 2})

    def test_env_unset_scan_reads_nothing(self):
        self.make_journal_files(self.tmpdir.name)
        stamper = self.run_scan(self.tmpdir.name, receipts_on=False)

        self.assertEqual(len(stamper.pending_commitments), 3)
        self.assertEqual(stamper.commitment_records, {})


if __name__ == "__main__":
    unittest.main()
