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

"""Observed is not processed (C4).

KnownBlocks records the headers the stamper has seen. Were __do_bitcoin
to take the list of new blocks as processed the moment it is returned
and read the bodies afterwards, one body fetch that raises (a transient
RPC error, which the loop logs and survives) would leave every block
after it unread, and among them the block that replaced a shallow
anchor's: the tree waiting at that height would never be put back to
pending, and the next pass, seeing no new headers, would save it as
mature against the new chain's height. The proof would name a block the
chain no longer holds, the receipt would be written, and nothing owed
any more; the public library rejects such a proof against the
replacement block.

The blocks whose bodies have not been read are a queue the stamper
owns (unprocessed_blocks). A fetch that raises leaves the block, and every
block after it, for the next pass; no tree is saved while a block is owed;
a queued block the chain has since replaced is dropped, its replacement
being among the new blocks. A restart forgets the queue with the trees
(C4): the commitments are read from the journal as pending.

Fault model: a block-body double that raises OSError once at a named
block, the injection asserted to have fired; a real journal, database,
checkpoint and receipts file; a restart as a fresh Stamper, running its
real loop, on the same calendar directory. Not a power cut, not a live
node.

The same orphan from the other side of the handoff
(Test_header_discovery_is_all_or_nothing): a remembered tip that
advanced header by header while the list of new blocks was local to
update_from_proxy would let a read that raises after the last header and
before the return leave the tip advanced and the blocks never queued,
and the next pass, finding no new blocks, would save the same orphan.
Discovery advances the tip with the list it returns and puts it back
when any read of the scan raises.
"""

import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from bitcoin.core import CTransaction, CTxIn, CTxOut, COutPoint, b2lx
from bitcoin.core.script import CScript, OP_RETURN
from opentimestamps.core.op import OpSHA256
from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.timestamp import Timestamp

from otsserver.calendar import Calendar
from otsserver.stamper import KnownBlocks, Stamper, TimestampTx, UnconfirmedTimestampTx, make_timestamp_from_block_tx, pending_markers


@contextlib.contextmanager
def calendar_fixture():
    """A real calendar (journal, LevelDB, checkpoint) and a receipts file
    beside it, receipts on. Yields (directory, calendar, receipts path)."""
    with tempfile.TemporaryDirectory(prefix='block-queue-') as tmp:
        d = Path(tmp)
        caldir = d / 'calendar'
        caldir.mkdir()
        receipts_dir = d / 'receipts'
        receipts_dir.mkdir()
        (caldir / 'uri').write_text('http://127.0.0.1:14788\n')
        (caldir / 'hmac-key').write_bytes(b'K' * 32)
        rp = receipts_dir / 'anchors.jsonl'
        with mock.patch.dict(os.environ, {'OTSD_ANCHOR_RECEIPTS': str(rp)}):
            cal = Calendar(str(caldir))
            try:
                yield d, cal, rp
            finally:
                close(cal)


def close(cal):
    cal.journal.append_fd.close()
    if cal.journal.record_counts and cal.journal.record_counts.fd is not None:
        os.close(cal.journal.record_counts.fd)
        cal.journal.record_counts.fd = None
    cal.db.db.close()


def stamper(cal, rp):
    """A Stamper through its constructor, its loop replaced by a no-op so
    the passes are driven by hand."""
    with mock.patch.object(Stamper, '_Stamper__loop'):
        s = Stamper(cal, threading.Event(), 12, 1, 6, 21600, 20000, 100)
        s.thread.join()
    s.anchor_receipts_path = str(rp) if rp else None
    return s


def submit(cal, seed=1, records=5):
    """One commitment through the real journal; returns its message."""
    ts = Timestamp(hashlib.sha256(bytes([seed])).digest())
    cal.submit(ts, records=records)
    (msg,) = [msg for msg, a in ts.all_attestations() if isinstance(a, PendingAttestation)]
    return msg


def anchor_tx(msg, seed=b'A'):
    """A transaction whose OP_RETURN carries the tip of a one-commitment
    tree, as the stamper's tree close would build it; that tip; and the
    leaf under it (linked: the save writes the leaf's path to the tip)."""
    leaf = Timestamp(msg)
    tip = leaf.ops.add(OpSHA256())
    tx = CTransaction([CTxIn(COutPoint(seed * 32, 0), nSequence=0xfffffffe)],
                      [CTxOut(0, CScript([OP_RETURN, tip.msg]))])
    return tx, tip, leaf


def mined(msg, height=102):
    """The tree the stamper holds for msg once its anchor is mined in a
    one-transaction block at height."""
    tx, tip, leaf = anchor_tx(msg)
    tree = TimestampTx(tx, tip, [leaf], 500, height, 5)
    tip.merge(make_timestamp_from_block_tx(tree, SimpleNamespace(vtx=[tx], hashMerkleRoot=tx.GetTxid()), height))
    return tree


def receipts(rp):
    return [json.loads(l) for l in rp.read_text().splitlines()] if rp.exists() else []


def block_hash(height):
    return hashlib.sha256(str(height).encode()).digest()


class Chain:
    """Just enough bitcoind for KnownBlocks and the body reads: a height
    to hash map, block bodies by hash (empty unless given), and one body
    fetch that raises at a named block."""

    def __init__(self):
        self.hashes = {100: b'a' * 32}
        self.blocks = {}
        self.fail_once = None
        self.reads = []

    def getblockcount(self):
        return max(self.hashes)

    def getbestblockhash(self):
        return self.hashes[max(self.hashes)]

    def getblockhash(self, height):
        try:
            return self.hashes[height]
        except KeyError:
            raise IndexError(height)

    def getblock(self, bh):
        self.reads.append(bh)
        if self.fail_once == bh:
            self.fail_once = None
            raise OSError('injected transient RPC disconnect')
        return self.blocks.get(bh, SimpleNamespace(vtx=[], hashMerkleRoot=b'Z' * 32))


def do_bitcoin(s):
    s._Stamper__do_bitcoin()


def shallow_anchor_then_reorg(cal, rp):
    """The scene every case starts from: one commitment anchored at height
    102 on a chain the stamper knows to 102; then a two-block reorg
    replaces 101 and 102 and the replacement chain grows to 107 before the
    next pass. Returns (msg, tree, stamper, chain)."""
    msg = submit(cal)
    tree = mined(msg)
    s = stamper(cal, rp)
    chain = Chain()
    s.known_blocks.update_from_proxy(chain)
    chain.hashes.update({101: b'b' * 32, 102: b'c' * 32})
    s.known_blocks.update_from_proxy(chain)
    s.txs_waiting_for_confirmation = {102: tree}
    s.commitment_idxs = {msg: 0}
    s.commitment_records = {msg: 5}
    s.journal_cursor = 1
    for height in range(101, 108):
        chain.hashes[height] = block_hash(height)
    return msg, tree, s, chain


def restarted(directory, chain, deadline=15):
    """A fresh Stamper on the calendar at directory, its real loop run
    against chain until it has scanned the journal and made one Bitcoin
    pass; returns (calendar, stamper) with the loop stopped."""
    cal = Calendar(str(Path(directory) / 'calendar'))
    exit_event = threading.Event()
    with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
        s = Stamper(cal, exit_event, 12, 1, 6, 21600, 20000, 100)
        end = time.time() + deadline
        while time.time() < end and not (s.journal_cursor is not None and s.known_blocks.best_block_height()):
            time.sleep(0.05)
        exit_event.set()
        s.thread.join(10)
    return cal, s


class Test_block_bodies_are_owned_until_read(unittest.TestCase):
    def test_a_failed_fetch_after_a_reorg_keeps_the_block_owed_and_saves_no_orphan(self):
        """The probe's fault case: the fetch of the first replacement block
        raises once. The pass stops with every block still queued and the
        tree still owned; the next pass reads the bodies, puts the tree
        back to pending, and saves nothing. Then the commitment is
        anchored again on the new chain and its saved proof names that
        block, which the public library accepts."""
        with calendar_fixture() as (d, cal, rp):
            msg, tree, s, chain = shallow_anchor_then_reorg(cal, rp)
            chain.fail_once = chain.hashes[101]
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                with self.assertRaises(OSError):
                    do_bitcoin(s)
                self.assertIsNone(chain.fail_once, 'the injected disconnect fired')
                self.assertEqual([b[0] for b in s.unprocessed_blocks], list(range(101, 108)), 'every block is still owed')
                self.assertEqual(s.txs_waiting_for_confirmation, {102: tree}, 'the tree is owned, not saved')
                self.assertNotIn(msg, cal)
                self.assertEqual(s.known_blocks.best_block_height(), 107, 'the headers were observed')
                # The next pass: the same headers, the bodies readable now.
                do_bitcoin(s)
            self.assertEqual(s.unprocessed_blocks, [])
            self.assertNotIn(msg, cal, 'no orphan proof')
            self.assertIn(msg, s.pending_commitments, 'the commitment is back in pending')
            self.assertEqual(s.txs_waiting_for_confirmation, {})
            self.assertEqual(receipts(rp), [])
            self.assertEqual(pending_markers(str(rp)), [])
            # Convergence: anchored again, mined at 108, deep at 113.
            tx, tip, _ = anchor_tx(msg, seed=b'B')
            s.unconfirmed_txs.append(UnconfirmedTimestampTx(tx, tip, 1, 500, 5))
            chain.hashes[108] = block_hash(108)
            chain.blocks[chain.hashes[108]] = SimpleNamespace(vtx=[tx], hashMerkleRoot=tx.GetTxid())
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                do_bitcoin(s)
                self.assertEqual(sorted(s.txs_waiting_for_confirmation), [108])
                self.assertNotIn(msg, s.pending_commitments)
                for height in range(109, 114):
                    chain.hashes[height] = block_hash(height)
                do_bitcoin(s)
            self.assertIn(msg, cal)
            saved = list(cal[msg].all_attestations())
            self.assertEqual([a.height for _, a in saved], [108])
            for digest, attestation in saved:
                attestation.verify_against_blockheader(digest, SimpleNamespace(hashMerkleRoot=tx.GetTxid(), nTime=123))
            self.assertEqual([r['txid'] for r in receipts(rp)], [b2lx(tx.GetTxid())])
            self.assertEqual(pending_markers(str(rp)), [])

    def test_the_readable_control_returns_the_commitment_to_pending(self):
        """No fault: the reorg is processed in one pass and the commitment
        is pending again, nothing saved."""
        with calendar_fixture() as (d, cal, rp):
            msg, tree, s, chain = shallow_anchor_then_reorg(cal, rp)
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                do_bitcoin(s)
                do_bitcoin(s)
            self.assertEqual(s.unprocessed_blocks, [])
            self.assertNotIn(msg, cal)
            self.assertIn(msg, s.pending_commitments)
            self.assertEqual(s.txs_waiting_for_confirmation, {})
            self.assertEqual(receipts(rp), [])
            self.assertEqual(len(chain.reads), 7, 'each body read once')

    def test_a_reorg_during_the_stall_drops_the_replaced_blocks_from_the_queue(self):
        """Between the failed pass and its retry the chain reorganises
        again, replacing 105 to 107. The retry queues the replacements
        and drops the replaced blocks unread; the tree at 102 is still put
        back to pending by the replacement of 102."""
        with calendar_fixture() as (d, cal, rp):
            msg, tree, s, chain = shallow_anchor_then_reorg(cal, rp)
            chain.fail_once = chain.hashes[101]
            stale = [chain.hashes[h] for h in (105, 106, 107)]
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                with self.assertRaises(OSError):
                    do_bitcoin(s)
                self.assertIsNone(chain.fail_once)
                for height in (105, 106, 107):
                    chain.hashes[height] = hashlib.sha256(b'again %d' % height).digest()
                do_bitcoin(s)
            self.assertEqual(s.unprocessed_blocks, [])
            self.assertEqual(s.known_blocks.best_block_height(), 107)
            read = set(chain.reads)
            self.assertTrue(all(chain.hashes[h] in read for h in range(101, 108)), 'every current block was read')
            self.assertFalse(any(h in read for h in stale), 'no replaced block was read')
            self.assertIn(msg, s.pending_commitments)
            self.assertNotIn(msg, cal)

    def test_a_restart_during_the_stall_reads_the_commitment_as_pending(self):
        """The failed pass, then a stop: the queue and the tree are memory
        and are gone. The next start scans the journal from the checkpoint
        (none: from 0), finds the commitment absent from the database and
        pending again, and saves nothing (C4, the restart case). Nothing
        is owed for the forgotten anchor: no marker, no receipt."""
        with calendar_fixture() as (d, cal, rp):
            msg, tree, s, chain = shallow_anchor_then_reorg(cal, rp)
            chain.fail_once = chain.hashes[101]
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                with self.assertRaises(OSError):
                    do_bitcoin(s)
            self.assertIsNone(chain.fail_once)
            close(cal)
            restored, fresh = restarted(d, chain)
            try:
                self.assertIsNone(fresh.failure)
                self.assertIn(msg, fresh.pending_commitments)
                self.assertEqual(fresh.txs_waiting_for_confirmation, {})
                self.assertEqual(fresh.unprocessed_blocks, [])
                self.assertNotIn(msg, restored)
                self.assertEqual(receipts(rp), [])
                self.assertEqual(pending_markers(str(rp)), [])
            finally:
                close(restored)
            cal.__init__(str(d / 'calendar'))   # the fixture's own close needs an open calendar


class Test_block_queue_with_a_doubled_chain(unittest.TestCase):
    """The same ownership on the doubles the stamper's other tests use
    (test_anchor_records.make_stamper): a fetch that raises at the third
    of five new blocks, then a pass with no new headers."""

    def test_a_fetch_that_raises_leaves_it_and_every_later_block_queued(self):
        from otsserver.tests.test_anchor_records import make_stamper
        s = make_stamper(None)
        s.txs_waiting_for_confirmation[101] = TimestampTx(anchor_tx(b'x' * 44)[0], Timestamp(b'\x01' * 32), [Timestamp(b'x' * 44)], 1, 101)
        s.commitment_idxs[b'x' * 44] = 0
        blocks = [(h, bytes([h]) * 32) for h in range(102, 107)]
        s.known_blocks.update_from_proxy.return_value = blocks

        def body(bh):
            if bh == blocks[2][1]:
                raise OSError('injected')
            return mock.Mock(vtx=[])
        with mock.patch('otsserver.stamper.make_proxy') as make_proxy:
            make_proxy.return_value.getblock.side_effect = body
            with self.assertRaises(OSError):
                do_bitcoin(s)
        self.assertEqual(s.unprocessed_blocks, blocks[2:], 'the failed block and every later one')
        s.calendar.add_commitment_timestamps.assert_not_called()
        self.assertIn(101, s.txs_waiting_for_confirmation)
        # No new headers; the bodies now read; the mature tree saved after them.
        s.known_blocks.update_from_proxy.return_value = []
        s.known_blocks.best_block_height.return_value = 106
        with mock.patch('otsserver.stamper.make_proxy') as make_proxy:
            make_proxy.return_value.getblock.return_value = mock.Mock(vtx=[])
            do_bitcoin(s)
        self.assertEqual(s.unprocessed_blocks, [])
        s.calendar.add_commitment_timestamps.assert_called_once()
        self.assertEqual(s.txs_waiting_for_confirmation, {})


class Test_header_discovery_is_all_or_nothing(unittest.TestCase):
    """A remembered tip that advanced header by header while the list of
    new blocks was local to update_from_proxy would let a read that
    raises after the last header and before the return (here the tip read
    that ends the scan, once 107 is appended) leave the tip at 107 and
    the blocks never returned, and the next pass, finding no new blocks,
    would read no body, put no tree back to pending, and save the
    orphaned tree against the new chain's height, receipted; the public
    library rejects that proof against the replacement block. Discovery
    advances the tip with the list it returns and puts it back when a
    read raises, so the next pass discovers the same blocks again and
    reads them before anything is called mature.

    Fault model: one OSError from the tip read once the scan has
    appended 107, the injection asserted to have fired; the real store,
    as above; then each read of the scan raising once on KnownBlocks
    alone."""

    def interrupted_scan(self, chain, s, fail):
        """chain.getbestblockhash raising once, when the scan has appended
        107 (the last replacement header) and asks the tip again to end.
        Returns the list the injection appends to when it fires."""
        original = chain.getbestblockhash
        fired = []

        def tip():
            if fail and not fired and s.known_blocks.best_block_height() == 107:
                fired.append(True)
                raise OSError('injected RPC disconnect during header discovery')
            return original()
        chain.getbestblockhash = tip
        return fired

    def test_a_raise_inside_discovery_puts_the_tip_back_and_the_next_pass_reads_every_body(self):
        with calendar_fixture() as (d, cal, rp):
            msg, tree, s, chain = shallow_anchor_then_reorg(cal, rp)
            fired = self.interrupted_scan(chain, s, True)
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                with self.assertRaises(OSError):
                    do_bitcoin(s)
                self.assertEqual(fired, [True], 'the injected disconnect fired')
                # Nothing was seen without being queued: the tip is where it was.
                self.assertEqual(s.known_blocks.best_block_height(), 102)
                self.assertEqual(s.known_blocks.best_block_hash(), b'c' * 32)
                self.assertEqual(s.unprocessed_blocks, [])
                self.assertEqual(chain.reads, [], 'no body read')
                self.assertEqual(s.txs_waiting_for_confirmation, {102: tree}, 'the tree is owned, not saved')
                self.assertNotIn(msg, cal)
                # The next pass: the same headers discovered again, the bodies read.
                do_bitcoin(s)
            self.assertEqual(s.known_blocks.best_block_height(), 107)
            self.assertEqual(s.unprocessed_blocks, [])
            self.assertEqual(len(chain.reads), 7, 'every replacement body read')
            self.assertNotIn(msg, cal, 'no orphan proof')
            self.assertIn(msg, s.pending_commitments, 'the commitment is back in pending')
            self.assertEqual(s.txs_waiting_for_confirmation, {})
            self.assertEqual(receipts(rp), [])
            self.assertEqual(pending_markers(str(rp)), [])

    def test_the_healthy_control_reads_the_replacement_bodies(self):
        with calendar_fixture() as (d, cal, rp):
            msg, tree, s, chain = shallow_anchor_then_reorg(cal, rp)
            fired = self.interrupted_scan(chain, s, False)
            with mock.patch('otsserver.stamper.make_proxy', return_value=chain):
                do_bitcoin(s)
                do_bitcoin(s)
            self.assertEqual(fired, [])
            self.assertEqual(len(chain.reads), 7)
            self.assertNotIn(msg, cal)
            self.assertIn(msg, s.pending_commitments)
            self.assertEqual(receipts(rp), [])

    def test_a_raise_at_any_read_of_the_scan_leaves_the_known_blocks_as_they_were(self):
        """KnownBlocks alone, on the chain double, known to 102 and then a
        two-block reorg grown to 105: the reorg check (the hash asked at
        102), a hash read mid-scan (104) and the tip read that ends the
        scan each raise once. After each the known blocks are the three
        the scan started from, and the next call returns the whole
        replacement chain, in order."""
        for boundary, at in (('getblockhash', 102), ('getblockhash', 104), ('getbestblockhash', None)):
            with self.subTest(boundary=boundary, at=at):
                chain = Chain()
                kb = KnownBlocks()
                kb.update_from_proxy(chain)
                chain.hashes.update({101: b'b' * 32, 102: b'c' * 32})
                self.assertEqual([tuple(b) for b in kb.update_from_proxy(chain)], [(101, b'b' * 32), (102, b'c' * 32)])
                for height in range(101, 106):
                    chain.hashes[height] = block_hash(height)
                original = getattr(chain, boundary)
                fired = []

                def read(*args, original=original, at=at):
                    if not fired and ((at is None and kb.best_block_height() == 105) or (args and args[0] == at)):
                        fired.append(True)
                        raise OSError('injected')
                    return original(*args)
                setattr(chain, boundary, read)
                with self.assertRaises(OSError):
                    kb.update_from_proxy(chain)
                self.assertEqual(fired, [True], 'the injection fired')
                self.assertEqual((kb.best_block_height(), kb.best_block_hash()), (102, b'c' * 32), 'as they were')
                new = kb.update_from_proxy(chain)
                self.assertEqual([tuple(b) for b in new], [(h, block_hash(h)) for h in range(101, 106)])
                self.assertEqual(kb.best_block_height(), 105)


if __name__ == "__main__":
    unittest.main()
