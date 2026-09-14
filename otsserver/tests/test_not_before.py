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

"""The not-before bound (green review 2026-09-11, item 10).

Calendar.submit appends the hash of the newest block the stamper has seen,
then sha256, immediately before the time prefix. A block hash cannot be
known before the block exists, so a commitment carrying it provably formed
after that block; the anchor's own attestation already says before which
block. The sha256 keeps the journal entry at its 44 bytes: the bound rides
inside the commitment path exactly as the per-submission nonce does, and
every verifier that follows append/sha256/prepend ops -- the
opentimestamps library the ots client is built on, and the stdlib parser
in ops/selfstamp.py that the client adapter shares -- follows it unchanged.

Fails on the pre-change code: the path from the tip goes straight to the
time prefix, and the calendar has no best_block_hash.
"""

import importlib.util
import os
import struct
import tempfile
import unittest

from bitcoin.core import lx
from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.op import OpAppend, OpPrepend, OpSHA256
from opentimestamps.core.serialize import (BytesDeserializationContext,
                                           BytesSerializationContext)
from opentimestamps.core.timestamp import Timestamp

from otsserver.calendar import Calendar, Journal
from otsserver.stamper import KnownBlocks

URI = 'http://127.0.0.1:14788'
BLOCK_A = '00000000000000000001' + 'a' * 44   # display order, as an explorer shows it
BLOCK_B = '00000000000000000001' + 'b' * 44
SELFSTAMP = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'ops', 'selfstamp.py')


class ChainProxy:
    """Just enough bitcoind for KnownBlocks: a height -> hash map"""

    def __init__(self):
        self.hashes = {}

    def add(self, height, display_hex):
        self.hashes[height] = lx(display_hex)

    def getblockcount(self):
        return max(self.hashes)

    def getbestblockhash(self):
        return self.hashes[max(self.hashes)]

    def getblockhash(self, height):
        try:
            return self.hashes[height]
        except KeyError:
            raise IndexError(height)


class FakeStamper:
    def __init__(self):
        self.known_blocks = KnownBlocks()


def make_calendar(path):
    os.makedirs(path)
    with open(os.path.join(path, 'uri'), 'w') as fd:
        fd.write(URI + '\n')
    with open(os.path.join(path, 'hmac-key'), 'wb') as fd:
        fd.write(b'\x01' * 32)
    return Calendar(path)


def walk(timestamp):
    """The linear op path below timestamp as [(op, msg)], to the attestation"""
    path = []
    while timestamp.ops:
        (op, child), = timestamp.ops.items()
        path.append((op, child.msg))
        timestamp = child
    return path, timestamp


def load_selfstamp():
    spec = importlib.util.spec_from_file_location('selfstamp', SELFSTAMP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Test_not_before_bound(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.calendar = make_calendar(os.path.join(self.tmpdir.name, 'calendar'))
        self.stamper = FakeStamper()
        self.calendar.stamper = self.stamper
        self.chain = ChainProxy()

    def see_block(self, height, display_hex):
        self.chain.add(height, display_hex)
        self.stamper.known_blocks.update_from_proxy(self.chain)

    def test_path_carries_the_stampers_best_block_beside_the_time_prefix(self):
        self.see_block(900000, BLOCK_A)
        tip = Timestamp(b'\x11' * 32)
        self.calendar.submit(tip)

        path, leaf = walk(tip)
        ops = [op for op, _ in path]
        self.assertEqual(ops[0], OpAppend(bytes.fromhex(BLOCK_A)),
                         "the bound is the best block, in display byte order")
        self.assertEqual(ops[1], OpSHA256())
        self.assertIsInstance(ops[2], OpPrepend)
        self.assertEqual(len(ops[2][0]), 4, "the time prefix follows the bound")
        self.assertIsInstance(ops[3], OpAppend)
        self.assertEqual(len(ops[3][0]), 8, "then the mac")
        self.assertEqual(len(ops), 4)
        self.assertEqual(leaf.attestations, {PendingAttestation(URI)})
        # The journal entry is the leaf's msg, and still 44 bytes: the
        # stamper's fill/anchor pipeline reads it exactly as before.
        entry = Journal(self.calendar.path + '/journal')[0]
        self.assertEqual(entry, leaf.msg)
        self.assertEqual(len(entry), Journal.COMMITMENT_SIZE)

    def test_the_bound_is_honest_it_moves_with_the_chain(self):
        self.see_block(900000, BLOCK_A)
        first = Timestamp(b'\x11' * 32)
        self.calendar.submit(first)
        self.see_block(900001, BLOCK_B)
        second = Timestamp(b'\x22' * 32)
        self.calendar.submit(second)

        self.assertEqual(walk(first)[0][0][0], OpAppend(bytes.fromhex(BLOCK_A)))
        self.assertEqual(walk(second)[0][0][0], OpAppend(bytes.fromhex(BLOCK_B)))
        # And it is exactly what the stamper knows, nothing invented.
        self.assertEqual(self.calendar.best_block_hash(),
                         self.stamper.known_blocks.best_block_hash()[::-1])

    def test_no_block_known_no_bound_one_warning_then_recovery(self):
        # Startup, or bitcoind unreachable: the commitment goes out without a
        # bound rather than with a fabricated one, warned once, and the
        # bound returns with the first block seen.
        with self.assertLogs(level='WARNING') as captured:
            self.calendar.submit(Timestamp(b'\x11' * 32))
            unbound = Timestamp(b'\x22' * 32)
            self.calendar.submit(unbound)
        warnings = [r for r in captured.records if 'not-before' in r.getMessage()]
        self.assertEqual(len(warnings), 1, captured.output)
        ops = [op for op, _ in walk(unbound)[0]]
        self.assertIsInstance(ops[0], OpPrepend)
        self.assertEqual(len(ops), 2)

        self.see_block(900000, BLOCK_A)
        with self.assertLogs(level='INFO') as recovered:
            bound = Timestamp(b'\x33' * 32)
            self.calendar.submit(bound)
        self.assertTrue(any('not-before' in r.getMessage() for r in recovered.records),
                        recovered.output)
        self.assertEqual(walk(bound)[0][0][0], OpAppend(bytes.fromhex(BLOCK_A)))

    def test_public_verifiers_follow_the_bound_unchanged(self):
        self.see_block(900000, BLOCK_A)
        digest = b'\x44' * 32
        # As the aggregator would: a leaf under a (one-leaf) tree, submitted.
        leaf = Timestamp(digest)
        tip = leaf.ops.add(OpAppend(b'\x99' * 16)).ops.add(OpSHA256())
        self.calendar.submit(tip)
        ctx = BytesSerializationContext()
        leaf.serialize(ctx)
        serialized = ctx.getbytes()

        # The opentimestamps library (what the ots client is built on)
        # deserializes the proof and lands on the pending attestation at the
        # journal entry.
        parsed = Timestamp.deserialize(BytesDeserializationContext(serialized), digest)
        self.assertEqual(parsed, leaf)
        (msg, attestation), = parsed.all_attestations()
        self.assertEqual(attestation, PendingAttestation(URI))
        entry = Journal(self.calendar.path + '/journal')[0]
        self.assertEqual(msg, entry)

        # The stdlib parser in ops/selfstamp.py (shared with the client
        # adapter) reads a .ots built from the same bytes to the same
        # commitment.
        selfstamp = load_selfstamp()
        proof = selfstamp.parse_ots(selfstamp.build_ots(digest, serialized))
        self.assertEqual(proof.commitment, entry)
        self.assertEqual(proof.attestation, ('pending', URI))


if __name__ == "__main__":
    unittest.main()
