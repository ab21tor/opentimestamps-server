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

"""The anchor-receipt pending marker (full-review J1, ruled 2026-09-08).

Before this change the receipt line was appended BEFORE the calendar save,
so a crash between the two re-anchored the same commitments under a new
txid and billed the records twice (the billing red-team's b1 W2 window).
Now the stamper writes a marker beside the receipts file — the receipt
plus one commitment to probe — before the save, appends the receipt after
it, and removes the marker. A leftover marker is settled at the next
start (and before any new marker): probe in the calendar → the receipt
is appended if its txid is not already on file; probe absent → the save
never happened, the commitments re-anchor with their own receipt, the
marker is discarded loudly. No crash can bill twice; at most one receipt
is lost, and the marker names it.
"""

import json
import os
import stat
import tempfile
import unittest
from unittest import mock

from bitcoin.core import b2lx
from opentimestamps.core.timestamp import Timestamp

import otsserver.stamper
from otsserver.stamper import Stamper, TimestampTx, marker_path, _write_pending_receipt
from otsserver.tests.test_anchor_receipts import make_stamper, make_tx, make_commitments


class FakeCalendar:
    """Membership + save, the two calls the receipt path makes"""
    def __init__(self):
        self.saved = set()
        self.on_save = None

    def __contains__(self, msg):
        return msg in self.saved

    def add_commitment_timestamps(self, timestamps, watermark=None):
        if self.on_save is not None:
            self.on_save()
        for ts in timestamps:
            self.saved.add(ts.msg)
        self.watermark = watermark


def mined(seed, commitments, height=850000, records=3):
    return TimestampTx(make_tx(seed), None, commitments, 200, height, records)


class Test_receipt_marker(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.receipts = os.path.join(self.tmpdir.name, 'anchor-receipts.jsonl')
        self.marker = marker_path(self.receipts)
        self.stamper = make_stamper(self.receipts)
        self.calendar = FakeCalendar()
        self.stamper.calendar = self.calendar

    def tearDown(self):
        self.tmpdir.cleanup()

    def lines(self):
        if not os.path.exists(self.receipts):
            return []
        with open(self.receipts) as fd:
            return [json.loads(l) for l in fd.read().splitlines() if l]

    def markers(self):
        """Every marker beside the receipts file (temporary and set-aside files excluded)"""
        return sorted(n for n in os.listdir(self.tmpdir.name)
                      if '.pending' in n and not n.endswith('.tmp') and '.corrupt-' not in n)

    def marker_of(self, tx):
        return marker_path(self.receipts, b2lx(tx.tx.GetTxid()))

    def save(self, tx):
        self.stamper._Stamper__save_confirmed_timestamp_tx(tx)

    def test_marker_before_save_receipt_after_marker_gone(self):
        commitments = make_commitments(3)
        seen = {}

        def during_save():
            seen['marker'] = os.path.exists(self.marker_of(tx))
            seen['receipts'] = self.lines()
            with open(self.marker_of(tx)) as fd:
                seen['body'] = json.load(fd)
        self.calendar.on_save = during_save

        tx = mined(1, commitments)
        self.save(tx)

        self.assertTrue(seen['marker'], 'marker must exist while the calendar save runs')
        self.assertEqual(seen['receipts'], [], 'no receipt line before the save')
        self.assertEqual(seen['body']['receipt']['txid'], b2lx(tx.tx.GetTxid()))
        self.assertIn(seen['body']['probe'], [c.msg.hex() for c in commitments])
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertEqual(self.markers(), [], 'marker removed after the receipt')

    def test_crash_before_save_discards_marker_no_receipt(self):
        commitments = make_commitments(3)
        self.calendar.on_save = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        tx = mined(1, commitments)
        with self.assertRaises(KeyboardInterrupt):
            self.save(tx)
        self.assertTrue(os.path.exists(self.marker_of(tx)))
        self.assertEqual(self.lines(), [])

        # The restart: the probe is not in the calendar, so nothing is owed
        # for this txid; the commitments re-anchor with their own receipt.
        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        with self.assertLogs(level='WARNING') as captured:
            fresh.settle_pending_receipt()
        self.assertEqual(self.markers(), [])
        self.assertEqual(self.lines(), [])
        self.assertTrue(any(b2lx(tx.tx.GetTxid()) in l and 'discard' in l for l in captured.output),
                        captured.output)

        self.calendar.on_save = None
        tx2 = mined(2, commitments)
        fresh._Stamper__save_confirmed_timestamp_tx(tx2)
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx2.tx.GetTxid())],
                         'exactly one receipt, for the anchor the calendar holds')

    def test_crash_after_save_before_receipt_recovers_it(self):
        commitments = make_commitments(3)
        tx = mined(1, commitments)
        with mock.patch('otsserver.stamper._append_anchor_receipt',
                        side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.save(tx)
        self.assertTrue(os.path.exists(self.marker_of(tx)))
        self.assertEqual(self.lines(), [])
        self.assertIn(commitments[0].msg, self.calendar)

        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        with self.assertLogs(level='WARNING') as captured:
            fresh.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertEqual(self.markers(), [])
        self.assertTrue(any('recovered' in l for l in captured.output), captured.output)

    def test_crash_after_receipt_before_unlink_writes_no_duplicate(self):
        commitments = make_commitments(3)
        tx = mined(1, commitments)
        with mock.patch('os.unlink', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.save(tx)
        self.assertTrue(os.path.exists(self.marker_of(tx)))
        self.assertEqual(len(self.lines()), 1)

        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        fresh.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertEqual(self.markers(), [])

    def test_receipt_write_failure_keeps_marker_and_recovers_later(self):
        commitments = make_commitments(3)
        tx = mined(1, commitments)
        with mock.patch('otsserver.stamper._append_anchor_receipt',
                        side_effect=OSError(28, 'No space left on device')):
            with self.assertLogs(level='WARNING'):
                self.save(tx)   # never breaks the stamp loop
        self.assertIn(commitments[0].msg, self.calendar)
        self.assertTrue(os.path.exists(self.marker_of(tx)), 'marker kept: the receipt is still owed')

        # The next anchor settles the leftover before writing its own marker.
        tx2 = mined(2, make_commitments(2))
        self.save(tx2)
        self.assertEqual([r['txid'] for r in self.lines()],
                         [b2lx(tx.tx.GetTxid()), b2lx(tx2.tx.GetTxid())])
        self.assertEqual(self.markers(), [])

    def test_no_marker_and_receipts_off_are_no_ops(self):
        self.stamper.settle_pending_receipt()
        self.assertEqual(self.markers(), [])
        off = make_stamper(None)
        off.calendar = self.calendar
        off.settle_pending_receipt()
        off._Stamper__save_confirmed_timestamp_tx(mined(1, make_commitments(1)))
        self.assertEqual(self.lines(), [])
        self.assertEqual(os.listdir(self.tmpdir.name), [])

    def test_unreadable_marker_is_set_aside_not_fatal(self):
        with open(self.marker, 'w') as fd:
            fd.write('not json')
        with self.assertLogs(level='WARNING'):
            self.stamper.settle_pending_receipt()
        self.assertFalse(os.path.exists(self.marker))
        self.assertEqual(self.markers(), [])
        self.assertTrue(any(n.startswith('anchor-receipts.jsonl.pending.corrupt-') for n in os.listdir(self.tmpdir.name)),
                        os.listdir(self.tmpdir.name))
        self.assertEqual(self.lines(), [])


class Test_receipt_durability(unittest.TestCase):
    """2026-09-15 review, P2 "successful file calls do not imply complete
    records": a short os.write was taken for a whole receipt and the marker
    removed. Now the append is a checked write-all loop, the file and its
    directory are fsynced, the marker goes only after that, and an
    incomplete tail left by an interrupted append is dropped before the
    next append with its receipt recovered from the marker: no completed
    receipt is ever duplicated, no outstanding marker discarded. Exception
    injection at the write boundary; not a power cut."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.receipts = os.path.join(self.tmpdir.name, 'anchor-receipts.jsonl')
        self.marker = marker_path(self.receipts)
        self.stamper = make_stamper(self.receipts)
        self.calendar = FakeCalendar()
        self.stamper.calendar = self.calendar

    def raw(self):
        with open(self.receipts, 'rb') as fd:
            return fd.read()

    def lines(self):
        return [json.loads(l) for l in self.raw().splitlines() if l]

    def markers(self):
        """Every marker beside the receipts file (temporary and set-aside files excluded)"""
        return sorted(n for n in os.listdir(self.tmpdir.name)
                      if '.pending' in n and not n.endswith('.tmp') and '.corrupt-' not in n)

    def marker_of(self, tx):
        return marker_path(self.receipts, b2lx(tx.tx.GetTxid()))

    def save(self, stamper, tx):
        stamper._Stamper__save_confirmed_timestamp_tx(tx)

    def receipt_for(self, tx):
        return {'txid': b2lx(tx.tx.GetTxid()), 'fee_sats': 200, 'commitments': len(tx.commitment_timestamps),
                'confirmed_height': 850000, 'confirmed_at': 1, 'records': 3}

    def leave_marker(self, tx):
        """The state after a save whose receipt never fully landed."""
        self.calendar.saved.add(tx.commitment_timestamps[0].msg)
        _write_pending_receipt(self.receipts, {'receipt': self.receipt_for(tx),
                                               'probe': tx.commitment_timestamps[0].msg.hex()})

    def test_a_short_write_is_completed_before_the_marker_goes(self):
        original = os.write
        calls = []

        def short_second(fd, data):
            calls.append(bytes(data))
            return original(fd, data[:8] if len(calls) == 2 else data)
        tx = mined(1, make_commitments(1))
        with mock.patch('otsserver.stamper.os.write', side_effect=short_second):
            self.save(self.stamper, tx)
        raw = self.raw()
        self.assertTrue(raw.endswith(b'\n'))
        self.assertEqual(raw.count(b'\n'), 1)
        self.assertEqual(json.loads(raw)['txid'], b2lx(tx.tx.GetTxid()))
        self.assertEqual(self.markers(), [], 'the marker goes only after the whole record is on disk')
        self.assertGreaterEqual(len(calls), 3, 'the short write was followed by the rest of the record')

    def test_an_incomplete_tail_is_dropped_and_its_receipt_recovered_from_the_marker(self):
        first = mined(1, make_commitments(2))
        self.save(self.stamper, first)
        whole = self.raw()
        # A later anchor was saved and its append interrupted mid-line:
        # part of a line, no newline, the marker still standing.
        second = mined(2, make_commitments(3))
        self.leave_marker(second)
        with open(self.receipts, 'ab') as fd:
            fd.write((json.dumps(self.receipt_for(second)) + '\n').encode()[:20])
        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        with self.assertLogs(level='WARNING') as captured:
            fresh.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()],
                         [b2lx(first.tx.GetTxid()), b2lx(second.tx.GetTxid())])
        self.assertTrue(self.raw().startswith(whole), 'the completed receipt is untouched')
        self.assertTrue(self.raw().endswith(b'\n'))
        self.assertEqual(self.markers(), [])
        self.assertTrue(any('incomplete' in l for l in captured.output), captured.output)
        # Recovering again changes nothing.
        fresh.settle_pending_receipt()
        self.assertEqual(len(self.lines()), 2)

    def test_a_line_that_lost_only_its_newline_is_written_once(self):
        tx = mined(1, make_commitments(1))
        self.leave_marker(tx)
        with open(self.receipts, 'wb') as fd:
            fd.write(json.dumps(self.receipt_for(tx)).encode())   # everything but the newline
        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        with self.assertLogs(level='WARNING'):
            fresh.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertEqual(self.raw().count(b'\n'), 1)
        self.assertEqual(self.markers(), [])

    def test_a_write_that_makes_no_progress_is_an_error_and_keeps_the_marker(self):
        original = os.write

        def stuck_receipt(fd, data):
            # The marker's body is written whole; the receipt line makes no progress.
            return original(fd, data) if bytes(data).startswith(b'{"receipt"') else 0
        tx = mined(1, make_commitments(1))
        with mock.patch('otsserver.stamper.os.write', side_effect=stuck_receipt):
            with self.assertLogs(level='WARNING') as captured:
                self.save(self.stamper, tx)
        self.assertTrue(os.path.exists(self.marker_of(tx)), 'the receipt is still owed')
        self.assertEqual(self.lines(), [])
        self.assertTrue(any('keeps it' in l for l in captured.output), captured.output)
        # Storage back: the next anchor settles the leftover first.
        tx2 = mined(2, make_commitments(1))
        self.save(self.stamper, tx2)
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid()), b2lx(tx2.tx.GetTxid())])
        self.assertEqual(self.markers(), [])

    def test_the_receipt_and_its_marker_are_fsynced_with_their_directory(self):
        synced = []
        real = os.fsync

        def record(fd):
            synced.append(stat.S_ISDIR(os.fstat(fd).st_mode))
            return real(fd)
        with mock.patch('otsserver.stamper.os.fsync', side_effect=record):
            self.save(self.stamper, mined(1, make_commitments(1)))
        self.assertGreaterEqual(synced.count(True), 2, 'the marker rename and the receipt append each fsync the directory')
        self.assertGreaterEqual(synced.count(False), 2, 'the marker file and the receipts file are fsynced')


class Test_marker_per_anchor(unittest.TestCase):
    """2026-09-15/16 review F08: one marker per anchor. Before this change
    there was one marker name; when settling an earlier anchor's marker
    failed (its receipt append raised), the later anchor went on, wrote its
    own receipt, and unlinked "the" marker: the earlier anchor's, whose
    receipt had never been written. Now a marker is named by its txid,
    settling is one marker at a time and a failure leaves that marker
    standing, and an anchor unlinks only its own. Exception injection at
    the write boundary; not a power cut."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.receipts = os.path.join(self.tmpdir.name, 'anchor-receipts.jsonl')
        self.stamper = make_stamper(self.receipts)
        self.calendar = FakeCalendar()
        self.stamper.calendar = self.calendar

    def lines(self):
        if not os.path.exists(self.receipts):
            return []
        with open(self.receipts) as fd:
            return [json.loads(l) for l in fd.read().splitlines() if l]

    def markers(self):
        return sorted(n for n in os.listdir(self.tmpdir.name)
                      if '.pending' in n and not n.endswith('.tmp') and '.corrupt-' not in n)

    def save(self, tx):
        self.stamper._Stamper__save_confirmed_timestamp_tx(tx)

    @staticmethod
    def anchor(seed, first_byte, n):
        """A mined tree whose commitments are distinct from every other anchor's"""
        return TimestampTx(make_tx(seed), None, [Timestamp(bytes([first_byte + i]) * 32) for i in range(n)],
                           200, 850000 + seed, 3)

    def test_a_later_anchors_success_never_removes_an_earlier_anchors_marker(self):
        a, b = self.anchor(1, 0x10, 2), self.anchor(2, 0x20, 3)
        txid_a, txid_b = b2lx(a.tx.GetTxid()), b2lx(b.tx.GetTxid())
        real_append = otsserver.stamper._append_anchor_receipt

        def only_a_fails(path, receipt):
            if receipt['txid'] == txid_a:
                raise OSError(5, 'synthetic failed append of the older receipt')
            return real_append(path, receipt)
        with mock.patch('otsserver.stamper._append_anchor_receipt', side_effect=only_a_fails):
            with self.assertLogs(level='WARNING'):
                self.save(a)                       # saved; receipt owed; marker stands
            self.assertEqual(len(self.markers()), 1)
            with self.assertLogs(level='WARNING'):
                self.save(b)                       # settling A fails again; B's own receipt lands
        self.assertIn(b.commitment_timestamps[0].msg, self.calendar, "B's save is never held up by A's receipt")
        self.assertEqual([r['txid'] for r in self.lines()], [txid_b])
        self.assertEqual(len(self.markers()), 1, "A's marker: the receipt is still owed")
        # Storage back: A is settled at the next start or before the next anchor.
        with self.assertLogs(level='WARNING') as captured:
            self.stamper.settle_pending_receipt()
        self.assertEqual(sorted(r['txid'] for r in self.lines()), sorted([txid_a, txid_b]))
        self.assertEqual(self.markers(), [])
        self.assertTrue(any('recovered' in l and txid_a in l for l in captured.output), captured.output)

    def test_two_owed_receipts_are_both_recovered(self):
        a, b = self.anchor(1, 0x10, 2), self.anchor(2, 0x20, 3)
        with mock.patch('otsserver.stamper._append_anchor_receipt',
                        side_effect=OSError(28, 'No space left on device')):
            with self.assertLogs(level='WARNING'):
                self.save(a)
            with self.assertLogs(level='WARNING'):
                self.save(b)
        self.assertEqual(self.lines(), [])
        self.assertEqual(len(self.markers()), 2, 'one marker per anchor still owed')
        with self.assertLogs(level='WARNING'):
            self.stamper.settle_pending_receipt()
        self.assertEqual(sorted(r['txid'] for r in self.lines()),
                         sorted([b2lx(a.tx.GetTxid()), b2lx(b.tx.GetTxid())]))
        self.assertEqual(self.markers(), [])
        # Settling again changes nothing.
        self.stamper.settle_pending_receipt()
        self.assertEqual(len(self.lines()), 2)

    def test_a_marker_from_before_per_anchor_names_is_settled(self):
        """Control for the upgrade: the single-name marker a calendar
        running the previous code may leave behind is read and settled."""
        tx = self.anchor(1, 0x10, 1)
        txid = b2lx(tx.tx.GetTxid())
        receipt = {'txid': txid, 'fee_sats': 200, 'commitments': 1, 'confirmed_height': 850001,
                   'confirmed_at': 1, 'records': 3}
        self.calendar.saved.add(tx.commitment_timestamps[0].msg)
        legacy = self.receipts + '.pending'
        with open(legacy, 'w') as fd:
            fd.write(json.dumps({'receipt': receipt, 'probe': tx.commitment_timestamps[0].msg.hex()}) + '\n')
        with self.assertLogs(level='WARNING'):
            self.stamper.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()], [txid])
        self.assertFalse(os.path.exists(legacy))
        self.assertEqual(self.markers(), [])

if __name__ == "__main__":
    unittest.main()
