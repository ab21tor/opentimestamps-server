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

"""journal.known-good is a checkpoint the stamper WRITES, not just reads.

The restart loop has always honoured journal.known-good as its scan start,
but nothing ever wrote it — so every restart rescanned the whole journal
from index 0, one calendar-membership check per entry, a cost that grows
with all history. After each confirmed anchor the stamper now persists the
lowest journal index whose commitment is not yet anchored (outstanding =
pending + mined-but-not-yet-deep trees; when none, the scan cursor itself):
everything below the checkpoint is in the calendar, so a restart may begin
there and the rescan cost becomes one anchor window, not the archive.

Write half fails on unchanged code (the file never appears). The honour
half pins the reader's contract: entries below the checkpoint are neither
read from the journal nor membership-checked against the calendar.
"""

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from opentimestamps.core.timestamp import Timestamp

from otsserver.stamper import Stamper, UnconfirmedTimestampTx

from otsserver.tests.test_anchor_records import (
    make_stamper, make_prev_tx, drive_broadcast, drive_confirmation,
    drive_depth,
)

HMAC_ZERO = b'\x00' * 8


def journal_entry(i):
    """A distinct 36-byte commitment; stored padded to COMMITMENT_SIZE."""
    return bytes([0x10 + i]) * 36


class Test_checkpoint_written(unittest.TestCase):
    """After a tree reaches min_confirmations, journal.known-good exists and
    holds the lowest still-outstanding journal index."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)

    def known_good(self):
        path = os.path.join(self.tmpdir.name, 'journal.known-good')
        if not os.path.exists(path):
            return None
        with open(path, 'r') as fd:
            return int(fd.read().strip())

    def make_checkpoint_stamper(self):
        stamper = make_stamper(receipts_path=None)
        stamper.calendar.path = self.tmpdir.name
        stamper.commitment_idxs = {}
        stamper.journal_cursor = None
        return stamper

    def seed(self, stamper, idxs):
        for i in idxs:
            commitment = journal_entry(i)
            stamper.pending_commitments.add(commitment)
            stamper.commitment_idxs[commitment] = i
        stamper.journal_cursor = max(idxs) + 1
        # An in-flight prior tx, the sibling suites' shape: drive_broadcast
        # goes down the RBF-replacement path, no wallet mocking needed.
        stamper.unconfirmed_txs.append(
            UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32),
                                   0, 100))

    def confirm_cycle(self, stamper, first_height=100):
        """broadcast → mined → depth: the full confirmed-anchor path, exactly
        as test_anchor_records drives it (min_confirmations=6: the tx mined
        at first_height+1 saves when height reaches first_height+6)."""
        drive_broadcast(stamper, first_height)
        drive_confirmation(stamper, first_height + 1)
        drive_depth(stamper, first_height + 6)

    def test_all_anchored_checkpoints_at_scan_cursor(self):
        stamper = self.make_checkpoint_stamper()
        self.seed(stamper, [0, 1, 2])
        self.assertIsNone(self.known_good())  # nothing confirmed yet
        self.confirm_cycle(stamper)
        # The whole tree anchored and nothing else is outstanding: everything
        # below the scan cursor is in the calendar.
        self.assertEqual(self.known_good(), 3)

    def test_outstanding_commitment_caps_the_checkpoint(self):
        stamper = self.make_checkpoint_stamper()
        self.seed(stamper, [0, 1, 2])
        drive_broadcast(stamper, 100)          # tree closes over idxs 0-2
        drive_confirmation(stamper, 101)       # mined; pending emptied
        # A late arrival lands at idx 3 while the tree waits for depth: it is
        # outstanding, so the checkpoint must stop below it.
        late = journal_entry(3)
        stamper.pending_commitments.add(late)
        stamper.commitment_idxs[late] = 3
        stamper.journal_cursor = 4
        drive_depth(stamper, 106)  # 106 - 6 + 1 == 101: the tree confirms
        self.assertEqual(self.known_good(), 3)

    def test_checkpoint_write_failure_never_breaks_confirmation(self):
        stamper = self.make_checkpoint_stamper()
        self.seed(stamper, [0, 1, 2])
        # An unwritable calendar dir: the checkpoint write must warn, not
        # raise — anchoring is the job, the checkpoint is a convenience.
        stamper.calendar.path = os.path.join(self.tmpdir.name, 'absent-dir')
        with self.assertLogs(level='WARNING') as captured:
            self.confirm_cycle(stamper)
        self.assertTrue(any('checkpoint' in r.getMessage().lower()
                            for r in captured.records), captured.output)
        # The anchor itself completed: the tree left the waiting map.
        self.assertEqual(stamper.txs_waiting_for_confirmation, {})


class RecordingCalendar:
    """Calendar double: records every membership probe, path like the real one."""

    def __init__(self, path, anchored):
        self.path = path
        self.anchored = set(anchored)
        self.checked = []

    def __contains__(self, commitment):
        self.checked.append(commitment)
        return commitment in self.anchored


class Test_checkpoint_honoured_on_restart(unittest.TestCase):
    """A restart's scan starts AT the checkpoint: entries below it are never
    journal-read, never membership-checked."""

    def test_scan_starts_at_checkpoint(self):
        with tempfile.TemporaryDirectory() as cal_path:
            with open(os.path.join(cal_path, 'journal'), 'wb') as fd:
                for i in range(5):
                    fd.write(journal_entry(i) + HMAC_ZERO)
            with open(os.path.join(cal_path, 'journal.known-good'), 'w') as fd:
                fd.write('3\n')

            # Entries 0-2 are anchored (below the checkpoint); 3-4 are not.
            calendar = RecordingCalendar(cal_path, [journal_entry(i)
                                                    for i in range(3)])
            exit_event = threading.Event()
            with mock.patch.object(Stamper, '_Stamper__do_bitcoin'):
                stamper = Stamper(calendar, exit_event,
                                  conf_target=12,
                                  relay_feerate=1,
                                  min_confirmations=6,
                                  min_tx_interval=600,
                                  max_fee=1000000,
                                  max_pending=100)
                try:
                    deadline = time.time() + 5
                    while (stamper.journal_cursor != 5
                           and time.time() < deadline):
                        time.sleep(0.05)
                finally:
                    exit_event.set()
                    stamper.thread.join(5)

            self.assertEqual(stamper.journal_cursor, 5)
            self.assertEqual(list(stamper.pending_commitments),
                             [journal_entry(3), journal_entry(4)])
            # The honour contract: nothing below the checkpoint was probed.
            self.assertEqual(sorted(calendar.checked),
                             sorted([journal_entry(3), journal_entry(4)]))


if __name__ == "__main__":
    unittest.main()
