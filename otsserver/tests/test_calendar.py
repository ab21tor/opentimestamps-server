# Copyright (C) 2016 The OpenTimestamps developers
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

import gc
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

from bitcoin.core import *

from opentimestamps.core.timestamp import *

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation

from otsserver.calendar import *
from otsserver.stamper import Stamper

class Test_LevelDbCalendar(unittest.TestCase):
    def test_creation(self):
        with tempfile.TemporaryDirectory() as db_path:
            cal = LevelDbCalendar(db_path)
            # A fresh calendar contains nothing.
            self.assertNotIn(b'foo', cal)

    def test_contains(self):
        with tempfile.TemporaryDirectory() as db_path:
            cal = LevelDbCalendar(db_path)

            t = Timestamp(b'foo')

            self.assertNotIn(b'foo', cal)
            cal.add_timestamps([t])
            self.assertIn(b'foo', cal)

    def test_chain_timestamp(self):
        """Add/retrieve a timestamp with multiple operations"""
        with tempfile.TemporaryDirectory() as db_path:
            cal = LevelDbCalendar(db_path)

            t1 = Timestamp(b'foo')
            t2 = t1.ops.add(OpAppend(b'bar'))
            t3 = t2.ops.add(OpAppend(b'baz'))

            cal.add_timestamps([t1])
            self.assertIn(b'foo', cal)
            self.assertIn(b'foobar', cal)
            self.assertIn(b'foobarbaz', cal)

            t1b = cal[b'foo']
            self.assertEqual(t1, t1b)

    def test_merkle_tree_timestamps(self):
        """Add/retrieve a merkle tree of timestamps"""
        with tempfile.TemporaryDirectory() as db_path:
            cal = LevelDbCalendar(db_path)

            roots = [Timestamp(bytes([i])) for i in range(256)]
            merkle_tip = make_merkle_tree(roots)

            for root in roots:
                cal.add_timestamps([root])

                self.assertIn(merkle_tip.msg, cal)

            for root in roots:
                retrieved_root = cal[root.msg]
                self.assertEqual(root, retrieved_root)


class Test_LevelDbCalendar_storage(unittest.TestCase):
    """Pinned when the binding moved from py-leveldb to plyvel:
    the two behaviours the rest of the server relies on and a binding could
    change. rpc.py answers 404 on the KeyError; the stamper's confirmed
    saves must be on disk before the receipt is written."""

    def test_missing_commitment_raises_keyerror(self):
        with tempfile.TemporaryDirectory() as db_path:
            cal = LevelDbCalendar(db_path)
            cal.add_timestamps([Timestamp(b'foo')])
            with self.assertRaises(KeyError):
                cal[b'absent']
            self.assertNotIn(b'absent', cal)
            self.assertIn(b'foo', cal)

    def test_sync_batch_is_readable_by_a_fresh_process(self):
        """Written in one process, read back in another: what a restart, and
        a restore from backup, rely on. Two child processes so no LevelDB
        lock is held across the boundary."""
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        with tempfile.TemporaryDirectory() as db_path:
            writer = """
from otsserver.calendar import LevelDbCalendar
from opentimestamps.core.timestamp import Timestamp
from opentimestamps.core.op import OpAppend
t = Timestamp(b'foo'); t.ops.add(OpAppend(b'bar'))
LevelDbCalendar(%r).add_timestamps([t])
""" % db_path
            reader = """
from otsserver.calendar import LevelDbCalendar
from opentimestamps.core.timestamp import Timestamp
from opentimestamps.core.op import OpAppend
cal = LevelDbCalendar(%r)
t = Timestamp(b'foo'); t.ops.add(OpAppend(b'bar'))
print(b'foo' in cal, b'foobar' in cal, cal[b'foo'] == t)
""" % db_path
            subprocess.run([sys.executable, '-c', writer], cwd=root, check=True, timeout=60)
            out = subprocess.run([sys.executable, '-c', reader], cwd=root, check=True, timeout=60,
                                 capture_output=True, text=True).stdout
            self.assertEqual(out.strip(), 'True True True')


class Test_storage_generation(unittest.TestCase):
    """Rebuilding an absent database beside a retained checkpoint must not
    skip old work: db/ carries a generation and a committed watermark,
    journal.known-good names the generation, and Calendar refuses to
    start when they disagree. No file timestamps are read anywhere."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, 'calendar')
        os.makedirs(self.path)
        with open(os.path.join(self.path, 'uri'), 'w') as fd:
            fd.write('http://127.0.0.1:14788\n')
        with open(os.path.join(self.path, 'hmac-key'), 'wb') as fd:
            fd.write(b'\x01' * 32)
        self.known_good = os.path.join(self.path, 'journal.known-good')

    def entry(self, i):
        return bytes([0x10 + i]) * 36

    def journal(self, n):
        with open(os.path.join(self.path, 'journal'), 'wb') as fd:
            for i in range(n):
                fd.write(self.entry(i) + b'\x00' * 8)

    def open(self):
        return Calendar(self.path)

    def close(self, cal):
        cal.db.db.close()

    def refuse(self):
        """Open, expecting the storage check to stop the process; returns the
        CRITICAL text. The half-built Calendar is released so its LevelDB
        lock is too."""
        code = None
        with self.assertLogs(level='CRITICAL') as captured:
            try:
                Calendar(self.path)
            except SystemExit as exc:
                code = exc.code
        gc.collect()
        self.assertEqual(code, 1, 'startup must refuse')
        text = '\n'.join(captured.output)
        self.assertIn('CALENDAR STORAGE INCONSISTENT', text)
        self.assertIn('Recovery', text)
        self.assertIn('delete', text)
        self.assertIn('later blocks', text)
        return text

    def test_a_new_database_carries_a_generation_and_watermark_zero(self):
        cal = self.open()
        self.assertRegex(cal.generation, '^[0-9a-f]{32}$')
        self.assertEqual(cal.db.watermark, 0)
        self.assertIsNone(cal.checkpoint)
        generation = cal.generation
        self.close(cal)
        cal = self.open()
        self.assertEqual(cal.generation, generation)
        self.close(cal)

    def test_the_watermark_is_committed_with_the_timestamps(self):
        cal = self.open()
        cal.add_commitment_timestamps([Timestamp(self.entry(0))], watermark=1)
        self.assertEqual(cal.db.watermark, 1)
        self.close(cal)
        cal = self.open()
        self.assertEqual(cal.db.watermark, 1)
        self.assertIn(self.entry(0), cal)
        self.close(cal)

    def test_meta_keys_are_invisible_as_commitments(self):
        from otsserver.calendar import META_GENERATION, META_WATERMARK
        cal = self.open()
        for key in (META_GENERATION, META_WATERMARK):
            self.assertNotIn(key, cal)
            with self.assertRaises(KeyError):
                cal[key]
        self.close(cal)

    def test_a_lost_database_beside_a_kept_checkpoint_is_refused(self):
        # Journal entry 0 exists, the checkpoint
        # says everything below 1 is anchored, db/ is absent.
        self.journal(1)
        with open(self.known_good, 'w') as fd:
            fd.write('1\n')
        text = self.refuse()
        self.assertIn('an index alone', text)
        self.assertIn('journal.known-good', text)

    def test_an_older_database_restored_beside_a_newer_checkpoint_is_refused(self):
        self.journal(2)
        cal = self.open()
        generation = cal.generation
        cal.add_commitment_timestamps([Timestamp(self.entry(0))], watermark=1)
        write_checkpoint(self.known_good, 1, generation)
        self.close(cal)
        backup = os.path.join(self.tmpdir.name, 'db-backup')
        shutil.copytree(os.path.join(self.path, 'db'), backup)
        cal = self.open()
        self.assertEqual(cal.checkpoint, 1)
        cal.add_commitment_timestamps([Timestamp(self.entry(1))], watermark=2)
        write_checkpoint(self.known_good, 2, generation)
        self.close(cal)
        # The restore: yesterday's db/ beside today's checkpoint.
        shutil.rmtree(os.path.join(self.path, 'db'))
        shutil.copytree(backup, os.path.join(self.path, 'db'))
        text = self.refuse()
        self.assertIn('older than the checkpoint', text)
        self.assertIn('watermark is 1', text)
        # The documented recovery: delete the checkpoint; the scan starts at 0.
        os.unlink(self.known_good)
        cal = self.open()
        self.assertIsNone(cal.checkpoint)
        self.assertEqual(cal.db.watermark, 1)
        self.assertIn(self.entry(0), cal)
        self.assertNotIn(self.entry(1), cal)
        self.close(cal)

    def test_a_checkpoint_of_another_generation_is_refused(self):
        cal = self.open()
        self.close(cal)
        write_checkpoint(self.known_good, 0, 'ab' * 16)
        text = self.refuse()
        self.assertIn('different lineage', text)

    def test_a_malformed_checkpoint_is_refused(self):
        for text in ('torn-or-corrupt\n', '1 xyz\n', '1 2 3\n', ''):
            with open(self.known_good, 'w') as fd:
                fd.write(text)
            self.assertIn('malformed', self.refuse())

    def test_a_checkpoint_from_before_generations_is_refused_whatever_the_database_holds(self):
        """Not adopted even when the entry below it is in the database.
        The whole transition, the one-time
        rescan and its stops included, is in
        test_restore_calendar.Test_checkpoint_from_before_generations."""
        from otsserver.calendar import META_GENERATION, META_WATERMARK
        self.journal(2)
        # A database written before generations: keys, no meta.
        cal = self.open()
        cal.add_commitment_timestamps([Timestamp(self.entry(0))])
        cal.db.db.delete(META_GENERATION)
        cal.db.db.delete(META_WATERMARK)
        self.close(cal)
        for index in (1, 2):   # the entry below it held, and not held
            with open(self.known_good, 'w') as fd:
                fd.write('%d\n' % index)
            text = self.refuse()
            self.assertIn('an index alone', text)
            self.assertIn('Recovery, once', text)
            with open(self.known_good) as fd:
                self.assertEqual(fd.read(), '%d\n' % index, 'a refused start rewrites nothing')


class Test_journal_boundary(unittest.TestCase):
    """A checkpoint saying "everything below 3 is in the database" about a
    journal that is no longer the one it describes: were a missing
    journal recreated or a shorter one accepted, the scan would start at
    3 and every new submission would land at index 0, 1, 2, accepted
    durably and never anchored. The
    journal must reach the checkpoint and agree with the database at the
    entry below it, or the start is refused with the recovery text; a
    missing journal is never created while a checkpoint names an index
    above 0. The control drives a real stamper fill pass over a coherent
    restart and sees the new submission become pending."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = os.path.join(self.tmpdir.name, 'calendar')
        os.makedirs(self.path)
        with open(os.path.join(self.path, 'uri'), 'w') as fd:
            fd.write('http://127.0.0.1:14788\n')
        with open(os.path.join(self.path, 'hmac-key'), 'wb') as fd:
            fd.write(b'\x01' * 32)
        self.journal_path = os.path.join(self.path, 'journal')
        self.known_good = os.path.join(self.path, 'journal.known-good')
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop('OTSD_ANCHOR_RECEIPTS', None)

    def close(self, cal):
        cal.journal.append_fd.close()
        cal.db.db.close()

    def checkpointed_calendar(self):
        """Three entries submitted, saved and checkpointed at 3; closed."""
        cal = Calendar(self.path)
        entries = []
        for n in range(3):
            msg = bytes([n + 1]) * 44
            cal.journal.submit(msg)
            ts = Timestamp(msg)
            ts.attestations.add(BitcoinBlockHeaderAttestation(1))
            entries.append(ts)
        cal.add_commitment_timestamps(entries, watermark=3)
        write_checkpoint(self.known_good, 3, cal.generation)
        self.close(cal)
        with open(self.journal_path, 'rb') as fd:
            return fd.read()

    def refuse(self):
        code = None
        with self.assertLogs(level='CRITICAL') as captured:
            try:
                Calendar(self.path)
            except SystemExit as exc:
                code = exc.code
        gc.collect()
        self.assertEqual(code, 1, 'startup must refuse')
        text = '\n'.join(captured.output)
        self.assertIn('CALENDAR STORAGE INCONSISTENT', text)
        self.assertIn('Recovery', text)
        return text

    def test_a_journal_shorter_than_the_checkpoint_is_refused_and_not_recreated(self):
        whole = self.checkpointed_calendar()
        for kept in (1, 0):
            with self.subTest(entries_kept=kept):
                with open(self.journal_path, 'wb') as fd:
                    fd.write(whole[:kept * Journal.COMMITMENT_SIZE])
                if kept == 0:
                    os.unlink(self.journal_path)
                text = self.refuse()
                self.assertIn('journal', text)
                self.assertIn('holds %d entr' % kept, text)
                if kept == 0:
                    self.assertFalse(os.path.exists(self.journal_path),
                                     'a refused start must not create the journal it found missing')
        # The documented recovery: delete the checkpoint; the scan starts at 0
        # over whatever journal there is.
        os.unlink(self.known_good)
        cal = Calendar(self.path)
        self.assertIsNone(cal.checkpoint)
        self.close(cal)

    def test_a_journal_of_another_lineage_is_refused(self):
        self.checkpointed_calendar()
        with open(self.journal_path, 'wb') as fd:
            for i in range(3):
                fd.write(bytes([0x40 + i]) * 36 + b'\x00' * 8)
        text = self.refuse()
        self.assertIn('does not hold journal entry 2', text)

    def test_a_coherent_restart_scans_the_next_submission(self):
        """Control:
        with the journal intact the restart is accepted, the checkpoint
        honoured, and a submission made after it is read by the stamper's
        first fill pass (Bitcoin stubbed, nothing else)."""
        self.checkpointed_calendar()
        cal = Calendar(self.path)
        self.assertEqual(cal.checkpoint, 3)
        cal.submit(Timestamp(b'n' * 32))
        reader = Journal(self.journal_path)
        try:
            new_commitment = reader[3]
        finally:
            reader.read_fd.close()
        self.assertNotIn(new_commitment, cal)
        stop = threading.Event()
        with mock.patch.object(Stamper, '_Stamper__do_bitcoin', lambda s: stop.set()):
            stamper = Stamper(cal, stop, 12, 1, 6, 21600, 20000, 100)
            stamper.thread.join(5)
        self.assertFalse(stamper.thread.is_alive())
        self.assertEqual(stamper.journal_cursor, 4)
        self.assertIn(new_commitment, stamper.pending_commitments)
        self.assertTrue(stamper.is_pending(new_commitment))
        self.close(cal)

if __name__ == "__main__":
    unittest.main()
