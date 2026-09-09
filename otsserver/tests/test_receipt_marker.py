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
import tempfile
import unittest
from unittest import mock

from bitcoin.core import b2lx

from otsserver.stamper import Stamper, TimestampTx, marker_path
from otsserver.tests.test_anchor_receipts import make_stamper, make_tx, make_commitments


class FakeCalendar:
    """Membership + save, the two calls the receipt path makes"""
    def __init__(self):
        self.saved = set()
        self.on_save = None

    def __contains__(self, msg):
        return msg in self.saved

    def add_commitment_timestamps(self, timestamps):
        if self.on_save is not None:
            self.on_save()
        for ts in timestamps:
            self.saved.add(ts.msg)


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

    def save(self, tx):
        self.stamper._Stamper__save_confirmed_timestamp_tx(tx)

    def test_marker_before_save_receipt_after_marker_gone(self):
        commitments = make_commitments(3)
        seen = {}

        def during_save():
            seen['marker'] = os.path.exists(self.marker)
            seen['receipts'] = self.lines()
            with open(self.marker) as fd:
                seen['body'] = json.load(fd)
        self.calendar.on_save = during_save

        tx = mined(1, commitments)
        self.save(tx)

        self.assertTrue(seen['marker'], 'marker must exist while the calendar save runs')
        self.assertEqual(seen['receipts'], [], 'no receipt line before the save')
        self.assertEqual(seen['body']['receipt']['txid'], b2lx(tx.tx.GetTxid()))
        self.assertIn(seen['body']['probe'], [c.msg.hex() for c in commitments])
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertFalse(os.path.exists(self.marker), 'marker removed after the receipt')

    def test_crash_before_save_discards_marker_no_receipt(self):
        commitments = make_commitments(3)
        self.calendar.on_save = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
        tx = mined(1, commitments)
        with self.assertRaises(KeyboardInterrupt):
            self.save(tx)
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(self.lines(), [])

        # The restart: the probe is not in the calendar, so nothing is owed
        # for this txid; the commitments re-anchor with their own receipt.
        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        with self.assertLogs(level='WARNING') as captured:
            fresh.settle_pending_receipt()
        self.assertFalse(os.path.exists(self.marker))
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
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(self.lines(), [])
        self.assertIn(commitments[0].msg, self.calendar)

        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        with self.assertLogs(level='WARNING') as captured:
            fresh.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertFalse(os.path.exists(self.marker))
        self.assertTrue(any('recovered' in l for l in captured.output), captured.output)

    def test_crash_after_receipt_before_unlink_writes_no_duplicate(self):
        commitments = make_commitments(3)
        tx = mined(1, commitments)
        with mock.patch('os.unlink', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.save(tx)
        self.assertTrue(os.path.exists(self.marker))
        self.assertEqual(len(self.lines()), 1)

        fresh = make_stamper(self.receipts)
        fresh.calendar = self.calendar
        fresh.settle_pending_receipt()
        self.assertEqual([r['txid'] for r in self.lines()], [b2lx(tx.tx.GetTxid())])
        self.assertFalse(os.path.exists(self.marker))

    def test_receipt_write_failure_keeps_marker_and_recovers_later(self):
        commitments = make_commitments(3)
        tx = mined(1, commitments)
        with mock.patch('otsserver.stamper._append_anchor_receipt',
                        side_effect=OSError(28, 'No space left on device')):
            with self.assertLogs(level='WARNING'):
                self.save(tx)   # never breaks the stamp loop
        self.assertIn(commitments[0].msg, self.calendar)
        self.assertTrue(os.path.exists(self.marker), 'marker kept: the receipt is still owed')

        # The next anchor settles the leftover before writing its own marker.
        tx2 = mined(2, make_commitments(2))
        self.save(tx2)
        self.assertEqual([r['txid'] for r in self.lines()],
                         [b2lx(tx.tx.GetTxid()), b2lx(tx2.tx.GetTxid())])
        self.assertFalse(os.path.exists(self.marker))

    def test_no_marker_and_receipts_off_are_no_ops(self):
        self.stamper.settle_pending_receipt()
        self.assertFalse(os.path.exists(self.marker))
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
        self.assertTrue(any(n.startswith('anchor-receipts.jsonl.pending.') for n in os.listdir(self.tmpdir.name)),
                        os.listdir(self.tmpdir.name))
        self.assertEqual(self.lines(), [])


if __name__ == "__main__":
    unittest.main()
