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

"""A mature tree stays until its save is durable (2026-09-15 review, P1
"confirmation is removed from memory before storage succeeds").

Before this change __do_bitcoin popped the tree that had reached
min_confirmations and then saved it; a save that raised (a storage error)
was logged by the loop and the tree was gone from every queue, its
journal entries already behind the scan cursor, so the next block never
retried it. Now a tree leaves txs_waiting_for_confirmation only after its
save has returned (LevelDB's synchronous write), a failed save is logged
once and the tree kept, and every pass, with or without a new block,
retries every mature unsaved tree until it lands. The checkpoint the save
makes true is committed in the same batch as the timestamps
(Calendar.add_commitment_timestamps watermark) and is what the file
carries.

Fails on the pre-change code: the tree is dropped and never retried.
"""

import os
import tempfile
import unittest
from unittest import mock

from opentimestamps.core.timestamp import Timestamp

from otsserver.calendar import Calendar, read_checkpoint
from otsserver.stamper import UnconfirmedTimestampTx
from otsserver.tests.test_anchor_records import (
    make_stamper, make_prev_tx, drive_broadcast, drive_confirmation, drive_depth, do_bitcoin,
)


def drive_idle(stamper, best_height):
    """A pass with no new block: the retry path."""
    stamper.known_blocks.update_from_proxy.return_value = []
    stamper.known_blocks.best_block_height.return_value = best_height
    with mock.patch('otsserver.stamper.make_proxy') as make_proxy:
        make_proxy.return_value.getblock.return_value = mock.Mock(vtx=[])
        do_bitcoin(stamper)


def confirmed_tree(stamper, commitment, idx, first_height):
    """One commitment through broadcast and mining; the tree waits at first_height + 1."""
    stamper.pending_commitments.add(commitment)
    stamper.commitment_idxs[commitment] = idx
    stamper.unconfirmed_txs.append(UnconfirmedTimestampTx(make_prev_tx(), Timestamp(bytes([idx + 1]) * 32), 0, 100))
    drive_broadcast(stamper, first_height)
    drive_confirmation(stamper, first_height + 1)


class Test_mature_tree_saves(unittest.TestCase):
    def test_a_failed_save_keeps_the_tree_and_every_pass_retries_it(self):
        s = make_stamper(None)
        confirmed_tree(s, b'x' * 44, 0, 100)
        self.assertIn(101, s.txs_waiting_for_confirmation)
        s.calendar.add_commitment_timestamps.side_effect = OSError('temporary storage error')
        with self.assertLogs(level='ERROR') as captured:
            drive_depth(s, 106)
        self.assertIn(101, s.txs_waiting_for_confirmation, 'kept until the save is durable')
        self.assertTrue(any('kept and retried' in r.getMessage() for r in captured.records), captured.output)
        self.assertEqual(s.commitment_idxs, {b'x' * 44: 0}, 'the journal index stays outstanding')
        # No new block: retried all the same, and quietly (warned once).
        with self.assertNoLogs(level='ERROR'):
            drive_idle(s, 106)
        self.assertEqual(s.calendar.add_commitment_timestamps.call_count, 2)
        self.assertIn(101, s.txs_waiting_for_confirmation)
        # Storage back: the save lands, the tree leaves, recovery logged once.
        s.calendar.add_commitment_timestamps.side_effect = None
        with self.assertLogs(level='INFO') as captured:
            drive_idle(s, 106)
        self.assertTrue(any('succeeding again' in r.getMessage() for r in captured.records), captured.output)
        self.assertEqual(s.txs_waiting_for_confirmation, {})
        self.assertEqual(s.commitment_idxs, {})
        self.assertEqual(s.calendar.add_commitment_timestamps.call_count, 3)

    def test_every_mature_tree_is_saved_oldest_first(self):
        s = make_stamper(None)
        confirmed_tree(s, b'x' * 44, 0, 100)    # waits at 101
        confirmed_tree(s, b'y' * 44, 1, 102)    # waits at 103
        self.assertEqual(sorted(s.txs_waiting_for_confirmation), [101, 103])
        s.calendar.add_commitment_timestamps.side_effect = OSError('storage')
        with self.assertLogs(level='ERROR'):
            drive_depth(s, 108)   # both are deep enough; both fail; both kept
        self.assertEqual(sorted(s.txs_waiting_for_confirmation), [101, 103])
        s.calendar.add_commitment_timestamps.side_effect = None
        drive_idle(s, 108)
        self.assertEqual(s.txs_waiting_for_confirmation, {})
        saved = [c.args[0][0].msg for c in s.calendar.add_commitment_timestamps.call_args_list[2:]]
        self.assertEqual(saved, [b'x' * 44, b'y' * 44])

    def test_the_committed_watermark_is_the_checkpoint_on_file(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, 'uri'), 'w') as fd:
                fd.write('http://127.0.0.1:14788\n')
            with open(os.path.join(d, 'hmac-key'), 'wb') as fd:
                fd.write(b'\x01' * 32)
            calendar = Calendar(d)
            s = make_stamper(None)
            s.calendar = calendar
            confirmed_tree(s, b'x' * 44, 0, 100)
            late = b'y' * 44                      # outstanding: caps the checkpoint
            s.pending_commitments.add(late)
            s.commitment_idxs[late] = 3
            s.journal_cursor = 4
            drive_depth(s, 106)
            self.assertEqual(s.txs_waiting_for_confirmation, {})
            self.assertIn(b'x' * 44, calendar)
            self.assertEqual(calendar.db.watermark, 3, 'committed in the batch with the timestamps')
            self.assertEqual(read_checkpoint(os.path.join(d, 'journal.known-good')), (3, calendar.generation))
            calendar.db.db.close()
            # A restart agrees with itself.
            again = Calendar(d)
            self.assertEqual(again.checkpoint, 3)
            self.assertEqual(again.db.watermark, 3)
            again.db.db.close()


if __name__ == "__main__":
    unittest.main()
