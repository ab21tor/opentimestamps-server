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

import os
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


if __name__ == "__main__":
    unittest.main()
