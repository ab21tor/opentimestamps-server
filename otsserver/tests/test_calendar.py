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
import unittest

from bitcoin.core import *

from opentimestamps.core.timestamp import *

from otsserver.calendar import *

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
    """Pinned when the binding moved from py-leveldb to plyvel (2026-09-14):
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
    """2026-09-15 review, P1 "rebuilding an absent database with a retained
    checkpoint skips old work": db/ now carries a generation and a committed
    watermark, journal.known-good names the generation, and Calendar refuses
    to start when they disagree. No file timestamps are read anywhere."""

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
        # The review's reproduction: journal entry 0 exists, the checkpoint
        # says everything below 1 is anchored, db/ is absent.
        self.journal(1)
        with open(self.known_good, 'w') as fd:
            fd.write('1\n')
        text = self.refuse()
        self.assertIn('predates this database', text)
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

    def test_a_checkpoint_from_before_generations_is_adopted_only_when_the_entry_below_it_is_held(self):
        from otsserver.calendar import META_GENERATION, META_WATERMARK
        self.journal(2)

        def strip_generation():
            # A database written before generations: keys, no meta.
            cal = self.open()
            cal.add_commitment_timestamps([Timestamp(self.entry(0))])
            cal.db.db.delete(META_GENERATION)
            cal.db.db.delete(META_WATERMARK)
            self.close(cal)
        strip_generation()
        with open(self.known_good, 'w') as fd:
            fd.write('1\n')
        with self.assertLogs(level='WARNING') as captured:
            cal = self.open()
        self.assertEqual(cal.checkpoint, 1)
        self.assertEqual(cal.db.watermark, 1)
        self.assertRegex(cal.generation, '^[0-9a-f]{32}$')
        self.assertEqual(read_checkpoint(self.known_good), (1, cal.generation))
        self.assertTrue(any('migrated' in l for l in captured.output), captured.output)
        self.close(cal)
        # Entry 1 is not in the database, yet the checkpoint claims it: refused.
        strip_generation()
        with open(self.known_good, 'w') as fd:
            fd.write('2\n')
        self.assertIn('does not hold journal entry 1', self.refuse())


if __name__ == "__main__":
    unittest.main()
