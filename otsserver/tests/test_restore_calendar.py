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

"""Workflow four, the calendar's half (docs/contracts.md, section 10): what
the calendar does with its own files when they were copied while it ran
(R1), restored beside one another from different moments (R2), left by a
version from before database generations (R3), or carried to another host
(R5), and what each recovery does when it is itself stopped (R6). The
tools' half is test_restore_tools.

Everything here is real but Bitcoin: a Calendar on LevelDB, its journal and
sidecar, the receipts file and its markers, a Stamper that opens and fills
from them; the anchors are driven by the doubles test_anchor_records has.
The fault models, named again by each class:

- a sequential copy: each member of the set is read whole, each at a
  different point of one submission or of one anchor's save. The points
  are the boundaries between the writes, and a member at a point is that
  member in a copy of the whole tree taken there (the database copied
  while open, after its synchronous batch: a crash image);
- a table file missing from a copied database, as when compaction removes
  one between the copier's listing and its read;
- a member missing from a restore that stopped part way;
- a stop (faults.Stop) at the n-th call of a named function, each case
  from a fresh fixture, each injection asserted to have fired, each
  recovery stopped once more;
- an fsync that fails (an OSError) under the receipt's append.

None of it is a power cut, and nothing here ran on Linux."""

import contextlib
import gc
import json
import logging
import os
import shutil
import tempfile
import threading
import types
import unittest
from unittest import mock

import plyvel

from opentimestamps.core.notary import BitcoinBlockHeaderAttestation, PendingAttestation
from opentimestamps.core.timestamp import Timestamp

import otsserver.calendar
import otsserver.stamper
from otsserver.calendar import (Calendar, Journal, RecordCountsWriter, META_GENERATION, META_WATERMARK,
                                read_checkpoint, write_checkpoint)
from otsserver.stamper import Stamper, UnconfirmedTimestampTx, marker_path, pending_markers
from otsserver.tests.faults import Stop, fail_on_call, unreadable
from otsserver.tests.test_anchor_records import (make_prev_tx, make_stamper, drive_broadcast, drive_confirmation,
                                                 drive_depth)

PRIVATE = 'client-acme-private'   # stands in every path here for a name that would identify a client

RECEIPTS = 'anchor-receipts.jsonl'   # the receipts file's name: the operator's choice, and so a name to keep out of messages

# The members of the calendar's own set, by their place under a box.
MEMBERS = {'journal': 'calendar/journal', 'counts': 'calendar/journal.counts', 'db': 'calendar/db',
           'checkpoint': 'calendar/journal.known-good', 'receipts': 'receipts'}


def close(cal):
    """Release a calendar's files, and with them LevelDB's lock."""
    cal.journal.append_fd.close()
    if cal.journal.record_counts is not None and cal.journal.record_counts.fd is not None:
        os.close(cal.journal.record_counts.fd)
        cal.journal.record_counts.fd = None
    cal.db.db.close()


def submit(cal, n, records=1):
    """One accepted digest, as the aggregator hands it over. Returns the
    holder's pending proof."""
    proof = Timestamp(bytes([n]) * 32)
    cal.submit(proof, records=records)
    return proof


def commitment_of(proof):
    (commitment,) = [msg for msg, attestation in proof.all_attestations() if isinstance(attestation, PendingAttestation)]
    return commitment


def upgrade(proof, cal):
    """The calendar's answer merged into the holder's proof, as a client's
    upgrade does. Returns the Bitcoin attestations the proof then carries."""
    commitment = commitment_of(proof)

    def find(stamp):
        if stamp.msg == commitment:
            return stamp
        for sub in stamp.ops.values():
            found = find(sub)
            if found is not None:
                return found
    find(proof).merge(cal[commitment])
    return [a for _, a in proof.all_attestations() if isinstance(a, BitcoinBlockHeaderAttestation)]


def answer(cal, stamper, commitment):
    """What GET /timestamp/<commitment> says (rpc.py): 200 when the database
    holds it, else the stamper's word for it, else Not found."""
    if commitment in cal:
        return '200'
    return stamper.is_pending(commitment) or 'Not found'


def anchor(stamper, height):
    """One whole anchor over what is pending: broadcast, mined, deep enough,
    saved, receipted, checkpointed."""
    stamper.known_blocks = mock.Mock()
    stamper.unconfirmed_txs.append(UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32), 0, 100))
    drive_broadcast(stamper, height)
    drive_confirmation(stamper, height + 1)
    drive_depth(stamper, height + 6)


def journal_entries(box):
    journal = Journal(os.path.join(box, 'calendar', 'journal'))
    try:
        entries = []
        while True:
            try:
                entries.append(journal[len(entries)])
            except KeyError:
                return entries
    finally:
        journal.read_fd.close()


def receipts_path(box):
    return os.path.join(box, 'receipts', RECEIPTS)


def receipt_lines(box):
    """(parsed complete lines, bytes after the last newline)"""
    try:
        with open(receipts_path(box), 'rb') as fd:
            raw = fd.read()
    except FileNotFoundError:
        return [], b''
    whole, _, tail = raw.rpartition(b'\n')
    return [json.loads(line) for line in whole.split(b'\n') if line], tail


def markers(box):
    return [os.path.basename(p) for p in pending_markers(receipts_path(box))]


class CalendarCase(unittest.TestCase):
    """A box is <root>/calendar and <root>/receipts, the calendar's own
    set. The story every class starts from, with a copy of the tree kept at
    each point named on the right:

        digests 0, 1 accepted; anchor A saves them     'anchored-A'
        digest 2 accepted                              'entry-2'
        digest 3: its journal entry is durable         'entry-3-journal'
        digest 3: its count is written                 'entry-3'
        anchor B goes out over entries 2 and 3;
        digest 4 accepted while B waits for depth      'entry-4'
        B's save: the marker is written                'B-marker'
                  the batch lands, watermark 4         'B-saved'
                  the receipt is appended              'B-receipted'
                  the marker is removed                'B-unmarked'
                  the checkpoint is rewritten, 4       'B-checkpointed'

    Entry 4 is pending at the end, and its holder has a pending proof."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix=PRIVATE + '-')
        self.addCleanup(self.tmpdir.cleanup)
        self.env = mock.patch.dict(os.environ)
        self.env.start()
        self.addCleanup(self.env.stop)
        # Recoveries say a great deal, by design. The tests that read it use
        # assertLogs, which puts its own handler in place of this one.
        root = logging.getLogger()
        handlers, root.handlers = root.handlers, [logging.NullHandler()]
        self.addCleanup(setattr, root, 'handlers', handlers)
        self.box = os.path.join(self.tmpdir.name, 'box')
        for part in ('calendar', 'receipts'):
            os.makedirs(os.path.join(self.box, part))
        with open(os.path.join(self.box, 'calendar', 'uri'), 'w') as fd:
            fd.write('http://127.0.0.1:14788\n')
        with open(os.path.join(self.box, 'calendar', 'hmac-key'), 'wb') as fd:
            fd.write(b'\x01' * 32)
        self.proofs = {}
        self.txids = {}

    # --- a box, started the way otsd starts it --------------------------------------

    def open(self, box):
        os.environ['OTSD_ANCHOR_RECEIPTS'] = receipts_path(box)
        cal = Calendar(os.path.join(box, 'calendar'))
        self.addCleanup(close, cal)
        return cal

    def start(self, box):
        """What otsd does before it serves, as far as Bitcoin is not needed:
        the storage check, then the stamper's open (the markers settled)
        and its first fill pass. Returns (calendar, stamper); the storage
        check's refusal is a SystemExit, the stamper's is its `failure`."""
        cal = self.open(box)
        stop = threading.Event()
        with mock.patch.object(Stamper, '_Stamper__do_bitcoin', lambda s: stop.set()), \
                mock.patch.object(Stamper, 'check_anchors', lambda s, proxy=None: []):
            stamper = Stamper(cal, stop, 12, 1, 6, 21600, 1000000, 100)
            stamper.thread.join(10)
        self.assertFalse(stamper.thread.is_alive())
        return cal, stamper

    def started(self, box):
        cal, stamper = self.start(box)
        self.assertIsNone(stamper.failure)
        return cal, stamper

    def refused(self, box):
        """A start that must be refused, by the storage check (exit 1, before
        the listener) or by the stamper's open (the service stops).
        Returns the CRITICAL text."""
        with self.assertLogs(level='CRITICAL') as logs:
            try:
                cal, stamper = self.start(box)
            except SystemExit as exc:
                self.assertEqual(exc.code, 1)
            else:
                self.assertIsNotNone(stamper.failure, 'the start must be refused')
                close(cal)
        gc.collect()    # a half-built Calendar lets go of LevelDB's lock
        text = '\n'.join(logs.output)
        self.assertIn('CALENDAR STORAGE INCONSISTENT', text)
        self.assertIn('Recovery', text)
        return text

    def conserved(self, box, cal, stamper):
        """Every entry of the journal that is here is in the database or
        pending: nothing the copy holds is skipped."""
        for index, entry in enumerate(journal_entries(box)):
            self.assertTrue(entry in cal or entry in stamper.pending_commitments,
                            'journal entry %d is neither saved nor pending' % index)

    # --- the story, and the copies taken along it -----------------------------------

    def at(self, point):
        return os.path.join(self.tmpdir.name, 'at', point)

    def snap(self, point):
        shutil.copytree(self.box, self.at(point))

    @contextlib.contextmanager
    def snap_after(self, obj, name, point):
        real = getattr(obj, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            self.snap(point)
            return result
        with mock.patch.object(obj, name, wrapper):
            yield

    def story(self):
        cal = self.open(self.box)
        for n in (0, 1):
            self.proofs[n] = submit(cal, n)
        close(cal)
        cal, stamper = self.started(self.box)
        anchor(stamper, 100)
        self.snap('anchored-A')

        self.proofs[2] = submit(cal, 2)
        self.snap('entry-2')
        real_put = RecordCountsWriter.put

        def put(writer, idx, count):
            self.snap('entry-3-journal')    # the entry is durable, its count not yet written
            return real_put(writer, idx, count)
        with mock.patch.object(RecordCountsWriter, 'put', put):
            self.proofs[3] = submit(cal, 3)
        self.snap('entry-3')
        close(cal)

        cal, stamper = self.started(self.box)
        stamper.known_blocks = mock.Mock()
        stamper.unconfirmed_txs.append(UnconfirmedTimestampTx(make_prev_tx(), Timestamp(b'\xaa' * 32), 0, 100))
        drive_broadcast(stamper, 200)
        drive_confirmation(stamper, 201)
        self.proofs[4] = submit(cal, 4)
        self.snap('entry-4')
        with contextlib.ExitStack() as stack:
            stack.enter_context(self.snap_after(otsserver.stamper, '_write_pending_receipt', 'B-marker'))
            stack.enter_context(self.snap_after(cal, 'add_commitment_timestamps', 'B-saved'))
            stack.enter_context(self.snap_after(otsserver.stamper, '_append_anchor_receipt', 'B-receipted'))
            stack.enter_context(self.snap_after(otsserver.stamper.os, 'unlink', 'B-unmarked'))
            stack.enter_context(self.snap_after(otsserver.stamper, 'write_checkpoint', 'B-checkpointed'))
            drive_depth(stamper, 206)
        close(cal)
        lines, _ = receipt_lines(self.box)
        self.txids['A'], self.txids['B'] = [line['txid'] for line in lines]
        self.commitments = {n: commitment_of(proof) for n, proof in self.proofs.items()}

    def compose(self, name, base, **members):
        """A set in which each named member was read at its own point and
        every other at `base`; a member given as None is left out."""
        dest = os.path.join(self.tmpdir.name, name)
        shutil.copytree(self.at(base), dest)
        for member, point in members.items():
            target = os.path.join(dest, MEMBERS[member])
            if os.path.isdir(target):
                shutil.rmtree(target)
            elif os.path.exists(target):
                os.unlink(target)
            source = None if point is None else os.path.join(self.at(point), MEMBERS[member])
            if source is not None and os.path.isdir(source):
                shutil.copytree(source, target)
            elif source is not None and os.path.exists(source):
                shutil.copy2(source, target)
            elif member == 'receipts':
                os.makedirs(target)     # the directory is configuration; its files are the member
        return dest

    def txids_on_file(self, box):
        lines, tail = receipt_lines(box)
        self.assertEqual(tail, b'', 'the receipts file ends in a complete line')
        return [line['txid'] for line in lines]


class Test_a_copy_taken_while_the_calendar_writes(CalendarCase):
    """R1. Nothing in this tree takes a backup, so nothing here can call a
    copy one; what this code can do is refuse, at the next start, a set
    whose members it can see were read at different moments, and that is
    what these cases ask of it. Fault model: the sequential copy."""

    def setUp(self):
        super().setUp()
        self.story()

    def test_a_copy_taken_at_any_one_point_restores(self):
        """Control: a copy of the whole tree at one instant is what a stop
        leaves, and every recovery of the calendar applies to it."""
        for point in sorted(os.listdir(os.path.join(self.tmpdir.name, 'at'))):
            with self.subTest(point=point):
                box = self.compose('one-' + point, point)
                cal, stamper = self.started(box)
                self.conserved(box, cal, stamper)
                saved_b = self.commitments[2] in cal
                self.assertEqual(self.txids_on_file(box), [self.txids['A']] + ([self.txids['B']] if saved_b else []),
                                 'one receipt for each anchor the database holds, and no other')
                self.assertEqual(markers(box), [])
                close(cal)

    def test_the_skews_that_lose_nothing_start(self):
        """Controls: a journal and sidecar read after the database and its
        checkpoint (the rest is pending), and a checkpoint read before the
        database (a longer scan). Neither is refused, and neither skips an
        entry."""
        box = self.compose('journal-newer', 'anchored-A', journal='entry-4', counts='entry-4')
        cal, stamper = self.started(box)
        self.conserved(box, cal, stamper)
        self.assertEqual(list(stamper.pending_commitments), [self.commitments[n] for n in (2, 3, 4)])
        close(cal)
        box = self.compose('checkpoint-older', 'B-checkpointed', checkpoint='entry-4')
        cal, stamper = self.started(box)
        self.assertEqual((cal.checkpoint, cal.db.watermark), (2, 4))
        self.conserved(box, cal, stamper)
        self.assertEqual(list(stamper.pending_commitments), [self.commitments[4]])
        close(cal)

    def test_a_checkpoint_read_after_the_database_is_refused(self):
        """Control (the start's storage check): the database read before B's save,
        the checkpoint after it."""
        text = self.refused(self.compose('skew', 'entry-4', checkpoint='B-checkpointed'))
        self.assertIn('older than the checkpoint', text)

    def test_a_journal_read_before_the_checkpoint_is_refused_when_it_does_not_reach_it(self):
        """Control (the start's storage check): the journal and its sidecar read
        at three entries, the rest after B's checkpoint at 4."""
        text = self.refused(self.compose('skew', 'B-checkpointed', journal='entry-2', counts='entry-2'))
        self.assertIn('the journal holds 3 entries', text)

    def test_a_sidecar_read_after_the_journal_is_refused(self):
        """The journal was read at three entries and the sidecar a moment
        later, with entry 3's count in it. The sidecar is written only
        after its entry is durable, so it is a witness: the journal here
        has lost an entry that was acknowledged."""
        text = self.refused(self.compose('skew', 'entry-2', counts='entry-3'))
        self.assertIn('journal.counts holds a count for journal entry 3', text)
        self.assertIn('the journal holds 3 entries', text)
        self.assertIn('undercount', text)

    def test_a_journal_read_between_an_entry_and_its_count_is_coherent(self):
        """Control for the sidecar check: the entry durable and its count
        not yet written is what a stop leaves, and the count is unknown."""
        box = self.compose('between', 'entry-3-journal')
        cal, stamper = self.started(box)
        self.conserved(box, cal, stamper)
        self.assertNotIn(self.commitments[3], stamper.commitment_records, 'a lost count is unknown, never invented')
        close(cal)

    def test_a_receipt_read_after_the_database_with_its_marker_standing_is_refused(self):
        """The database was read before B's save; the receipts directory
        after B's receipt was appended and before its marker went. No stop
        leaves that: the receipt follows the save's synchronous batch.
        Discarding the marker as `nothing is owed` beside a receipt on
        file would send entries 2 and 3 on to a second anchor and a
        second receipt."""
        box = self.compose('skew', 'B-marker', receipts='B-receipted')
        text = self.refused(box)
        self.assertIn(self.txids['B'], text)
        self.assertIn('newer than db/', text)
        self.assertIn('a second time', text)
        self.assertEqual(markers(box), ['anchor-receipts.jsonl.pending.' + self.txids['B']], 'the marker still stands')
        self.assertEqual(self.txids_on_file(box), [self.txids['A'], self.txids['B']], 'and nothing was written')

    def test_a_receipt_read_after_the_database_without_its_marker_is_not_detected(self):
        """The stated limit of the case above: once the marker has gone
        nothing on file ties a receipt to a commitment, so receipts newer
        than the database start cleanly, and the records of the anchor the
        database lacks are anchored and receipted a second time."""
        box = self.compose('skew', 'entry-4', receipts='B-unmarked')
        cal, stamper = self.started(box)
        self.assertIn(self.commitments[2], stamper.pending_commitments)
        anchor(stamper, 300)
        second = self.txids_on_file(box)
        self.assertEqual(second[:2], [self.txids['A'], self.txids['B']])
        self.assertEqual(len(second), 3, 'a second receipt for the records of B: not detected, stated in R1')
        close(cal)

    def test_a_database_read_after_the_receipts_with_the_marker_in_the_copy_is_recovered(self):
        """Control: the receipts directory read while B's marker stood, the
        database after the save. That is what a stop leaves, and the
        receipt is recovered from the marker, once."""
        box = self.compose('skew', 'B-checkpointed', receipts='B-marker')
        cal, stamper = self.started(box)
        self.assertEqual(self.txids_on_file(box), [self.txids['A'], self.txids['B']])
        self.assertEqual(markers(box), [])
        close(cal)

    def test_a_database_read_after_the_receipts_without_the_marker_loses_the_receipt_unseen(self):
        """The other stated limit: the receipts directory read before B's
        marker was written, the database after the save. The anchor is
        saved and nothing owes its receipt: an undercount, the direction
        receipts may err in, and not detected."""
        box = self.compose('skew', 'B-checkpointed', receipts='entry-4')
        cal, stamper = self.started(box)
        self.assertIn(self.commitments[2], cal)
        self.assertEqual(self.txids_on_file(box), [self.txids['A']])
        self.assertEqual(markers(box), [])
        close(cal)

    def test_a_database_that_does_not_open_is_refused_with_the_recovery_and_no_path(self):
        """A table file missing from the copied database: refused with the
        recovery, never a traceback out of Calendar() with LevelDB's
        message, which names the directory."""
        box = self.compose('torn', 'B-checkpointed')
        tables = sorted(name for name in os.listdir(os.path.join(box, 'calendar', 'db')) if name.endswith('.ldb'))
        self.assertTrue(tables, 'the fixture must hold a table file')
        os.unlink(os.path.join(box, 'calendar', 'db', tables[0]))
        text = self.refused(box)
        self.assertIn('db/ does not open (CorruptionError)', text)
        self.assertIn('copied while the calendar ran', text)
        self.assertIn('later blocks', text)
        self.assertNotIn(PRIVATE, text)


class Test_an_older_journal_beside_a_newer_database(CalendarCase):
    """R2: what the bounded journal check of the start (C6) detects, and
    what it does not, each case pinned as it is. Fault model: members of
    the set taken from different points of the story."""

    def setUp(self):
        super().setUp()
        self.story()

    def test_detected_when_the_journal_does_not_reach_the_checkpoint(self):
        text = self.refused(self.compose('older', 'B-checkpointed', journal='entry-2', counts='entry-2'))
        self.assertIn('names journal index 4, but the journal holds 3 entries', text)

    def test_detected_when_the_sidecar_outlasts_the_journal(self):
        """Workflow four's addition: the journal reaches the checkpoint (4)
        and lacks entry 4; the sidecar, from the newer moment, still counts
        it."""
        box = self.compose('older', 'B-checkpointed')
        with open(os.path.join(box, 'calendar', 'journal'), 'r+b') as fd:
            fd.truncate(4 * Journal.COMMITMENT_SIZE)
        text = self.refused(box)
        self.assertIn('journal.counts holds a count for journal entry 4', text)

    def test_not_detected_when_the_older_journal_still_reaches_the_checkpoint(self):
        """The journal and its sidecar read at four entries, the database
        and its checkpoint (4) later: a prefix of the true journal that
        reaches the checkpoint. The start is clean. Entry 4 was
        acknowledged and is gone; what its holder sees is `Not found`
        where an entry that is owed says `Pending`, and no anchor changes
        that."""
        box = self.compose('older', 'B-checkpointed')
        for name in ('journal', 'journal.counts'):
            with open(os.path.join(box, 'calendar', name), 'r+b') as fd:
                fd.truncate(4 * (Journal.COMMITMENT_SIZE if name == 'journal' else 4))
        cal, stamper = self.started(box)
        self.assertEqual(cal.checkpoint, 4)
        self.assertEqual(answer(cal, stamper, self.commitments[4]), 'Not found')
        self.proofs[5] = submit(cal, 5)
        close(cal)
        cal, stamper = self.started(box)
        anchor(stamper, 300)
        self.assertEqual(answer(cal, stamper, commitment_of(self.proofs[5])), '200')
        self.assertEqual(answer(cal, stamper, self.commitments[4]), 'Not found')
        close(cal)

    def test_the_control_for_not_found_is_pending(self):
        """With the journal whole the same holder is told `Pending`, and the
        next anchor completes the proof."""
        box = self.compose('whole', 'B-checkpointed')
        cal, stamper = self.started(box)
        self.assertTrue(answer(cal, stamper, self.commitments[4]).startswith('Pending'))
        anchor(stamper, 300)
        self.assertEqual(answer(cal, stamper, self.commitments[4]), '200')
        close(cal)

    def test_not_detected_when_the_journal_differs_only_between_the_two_probes(self):
        """The check reads entry 0 and the entry below the checkpoint. An
        entry between them that is not the one the database holds passes;
        only a rescan reads it, and a rescan anchors it as it finds it."""
        box = self.compose('between', 'B-checkpointed')
        with open(os.path.join(box, 'calendar', 'journal'), 'r+b') as fd:
            fd.seek(1 * Journal.COMMITMENT_SIZE)
            fd.write(b'\x77' * 36 + b'\x00' * 8)
        cal, stamper = self.started(box)
        self.assertEqual(cal.checkpoint, 4)
        self.assertNotIn(b'\x77' * 36, stamper.pending_commitments, 'below the checkpoint nothing is read')
        close(cal)
        os.unlink(os.path.join(box, 'calendar', 'journal.known-good'))
        cal, stamper = self.started(box)
        self.assertIn(b'\x77' * 36, stamper.pending_commitments, 'the rescan reads what is there, and anchors it')
        close(cal)

    def test_without_a_checkpoint_nothing_about_the_journal_is_checked(self):
        """The documented way on after a refusal: the checkpoint deleted,
        the scan from 0 over the journal that is here. The entries this
        journal lacks are served while the database holds them (3) and
        `Not found` when it does not (4)."""
        box = self.compose('rescan', 'B-checkpointed', journal='entry-2', counts='entry-2', checkpoint=None)
        cal, stamper = self.started(box)
        self.assertIsNone(cal.checkpoint)
        self.conserved(box, cal, stamper)
        self.assertEqual(answer(cal, stamper, self.commitments[3]), '200')
        self.assertEqual(answer(cal, stamper, self.commitments[4]), 'Not found')
        close(cal)


class Test_a_restore_that_stopped_part_way(CalendarCase):
    """R6, a stop during the restore itself. Fault model: one member of
    the stopped set missing from the destination. Each case says whether
    the start refuses, goes on safely, or cannot tell."""

    def setUp(self):
        super().setUp()
        self.story()

    def test_each_missing_member(self):
        for member, expected in (('journal', 'the journal holds 0 entries'),
                                 ('db', 'different lineage'),
                                 ('counts', None), ('checkpoint', None), ('receipts', None)):
            with self.subTest(missing=member):
                box = self.compose('without-' + member, 'B-saved', **{member: None})
                if member == 'journal':
                    os.unlink(os.path.join(box, 'calendar', 'journal.counts'))   # else the sidecar speaks first
                if member in ('journal', 'db'):
                    if member == 'db':
                        # B-saved's checkpoint is still 2: a database made now is another generation
                        self.assertEqual(read_checkpoint(os.path.join(box, 'calendar', 'journal.known-good'))[0], 2)
                    self.assertIn(expected, self.refused(box))
                    continue
                cal, stamper = self.started(box)
                self.conserved(box, cal, stamper)
                if member == 'counts':
                    self.assertEqual(stamper.commitment_records, {}, 'counts lost are unknown: receipts undercount')
                if member == 'checkpoint':
                    self.assertIsNone(cal.checkpoint)
                if member == 'receipts':
                    # The owed receipt of B went with its marker, unseen: the stated limit of R1.
                    self.assertEqual(self.txids_on_file(box), [])
                else:
                    self.assertEqual(self.txids_on_file(box), [self.txids['A'], self.txids['B']])
                close(cal)

    def test_the_sidecar_without_its_journal_is_refused(self):
        text = self.refused(self.compose('no-journal', 'B-saved', journal=None, checkpoint=None))
        self.assertIn('journal.counts holds a count for journal entry 4, but the journal holds 0 entries', text)

    def test_a_restore_finished_after_a_refused_start_starts(self):
        """Convergence: the refused start wrote nothing the finished copy
        trips over; the journal it found missing was not created."""
        box = self.compose('late', 'B-saved', journal=None, counts=None)
        self.refused(box)
        self.assertFalse(os.path.exists(os.path.join(box, 'calendar', 'journal')))
        for member in ('journal', 'counts'):
            shutil.copy2(os.path.join(self.at('B-saved'), MEMBERS[member]), os.path.join(box, MEMBERS[member]))
        cal, stamper = self.started(box)
        self.conserved(box, cal, stamper)
        self.assertEqual(self.txids_on_file(box), [self.txids['A'], self.txids['B']])
        close(cal)


class Test_a_restore_onto_a_fresh_host(CalendarCase):
    """R2 and R5. The set is what a stop left after B's save and before its
    receipt: a receipt owed (the marker), an entry pending (4), a
    checkpoint behind the database (2 against 4). It is copied under
    another root, which stands for another host: nothing in the set names
    the first."""

    def setUp(self):
        super().setUp()
        self.story()

    def test_it_settles_the_owed_receipt_and_completes_the_pending_proofs(self):
        elsewhere = os.path.join(self.tmpdir.name, 'replacement', 'host-b')
        shutil.copytree(self.at('B-saved'), elsewhere)
        shutil.rmtree(os.path.join(self.tmpdir.name, 'at'))
        shutil.rmtree(self.box)                                  # the first host is gone

        cal, stamper = self.started(elsewhere)
        self.assertEqual(self.txids_on_file(elsewhere), [self.txids['A'], self.txids['B']], 'the owed receipt, once')
        self.assertEqual(markers(elsewhere), [])
        # Saved before the copy: complete from the restored database.
        for n in (0, 2, 3):
            self.assertTrue(upgrade(self.proofs[n], cal), 'proof %d' % n)
        # Owed at the copy: pending here, completed by this host's next anchor.
        self.assertTrue(answer(cal, stamper, self.commitments[4]).startswith('Pending'))
        anchor(stamper, 300)
        self.assertTrue(upgrade(self.proofs[4], cal))
        lines, _ = receipt_lines(elsewhere)
        self.assertEqual([line['txid'] for line in lines[:2]], [self.txids['A'], self.txids['B']])
        self.assertEqual((len(lines), lines[2]['commitments'], lines[2]['records']), (3, 1, 1))
        generation = cal.generation
        close(cal)

        # The generation travelled with the database, and the checkpoint this host wrote names it.
        self.assertEqual(read_checkpoint(os.path.join(elsewhere, 'calendar', 'journal.known-good')), (5, generation))
        cal, stamper = self.started(elsewhere)
        self.assertEqual((cal.checkpoint, cal.generation), (5, generation))
        self.assertEqual(self.txids_on_file(elsewhere), [line['txid'] for line in lines], 'a restart receipts nothing twice')
        close(cal)

    def test_what_is_carried_names_the_calendar_and_never_the_host(self):
        """The pending proof names the calendar's uri, which is carried; no
        file of the set holds the path it was made under."""
        self.assertEqual([a.uri for _, a in self.proofs[4].all_attestations()], ['http://127.0.0.1:14788'])
        first_root = self.tmpdir.name.encode()
        for directory, _, names in os.walk(self.at('B-saved')):
            for name in names:
                with open(os.path.join(directory, name), 'rb') as fd:
                    self.assertNotIn(first_root, fd.read(), name)


class Test_checkpoint_from_before_generations(CalendarCase):
    """R3. A calendar as an earlier release left it: a database
    without a generation and journal.known-good holding an index alone.
    It is refused whatever the database holds; the operator deletes the
    file, once; the start after that gives the database its generation with
    watermark 0 and scans from 0. Fault model for the stops: faults.Stop
    before and after the one batch the adoption is."""

    def setUp(self):
        super().setUp()
        cal = self.open(self.box)
        for n in (0, 1, 2):
            self.proofs[n] = submit(cal, n)
        close(cal)
        cal, stamper = self.started(self.box)
        anchor(stamper, 100)
        self.proofs[3] = submit(cal, 3)
        close(cal)
        self.known_good = os.path.join(self.box, 'calendar', 'journal.known-good')
        self.assertEqual(read_checkpoint(self.known_good)[0], 3)
        db = plyvel.DB(os.path.join(self.box, 'calendar', 'db'))
        db.delete(META_GENERATION)
        db.delete(META_WATERMARK)
        db.close()
        with open(self.known_good, 'w') as fd:
            fd.write('3\n')

    def meta(self):
        db = plyvel.DB(os.path.join(self.box, 'calendar', 'db'))
        try:
            return db.get(META_GENERATION), db.get(META_WATERMARK)
        finally:
            db.close()

    def test_it_is_refused_with_the_one_time_rescan_named_and_nothing_is_written(self):
        """The entry below the index IS in the database, which is not enough
        to adopt it (that would be the database stamped, then the file
        rewritten: two writes)."""
        before = journal_entries(self.box)
        text = self.refused(self.box)
        self.assertIn('an index alone (3)', text)
        self.assertIn('Recovery, once: delete journal.known-good', text)
        self.assertIn('rescans the whole journal from index 0', text)
        self.assertEqual(self.meta(), (None, None), 'the database is not stamped')
        with open(self.known_good) as fd:
            self.assertEqual(fd.read(), '3\n', 'the file is not rewritten')
        self.assertEqual(journal_entries(self.box), before)
        self.refused(self.box)      # and again: a refusal is not a step

    def test_the_format_that_replaced_it_is_not_one_the_old_reader_reads(self):
        """Old code, given the new file: the earlier stamper read the
        checkpoint as int(text.strip()) (c5f6545, stamper.py:925) with
        only FileNotFoundError caught. 'INDEX GENERATION' is not an int:
        that reader raises, in the stamper's thread, behind a listener
        that stays up (the defect fcbafb6 fixed). The expression is quoted
        here, not that revision run: it refuses, and not cleanly."""
        write_checkpoint(self.known_good, 3, 'ab' * 16)
        with open(self.known_good) as fd:
            text = fd.read()
        with self.assertRaises(ValueError):
            int(text.strip())

    def test_the_rescan_is_performed_once_and_loses_nothing(self):
        os.unlink(self.known_good)                  # the operator's step
        with self.assertLogs(level='INFO') as logs:
            cal, stamper = self.started(self.box)
        self.assertTrue(any('stamped with generation' in line for line in logs.output))
        generation = cal.generation
        self.assertRegex(generation, '^[0-9a-f]{32}$')
        self.assertEqual((cal.checkpoint, cal.db.watermark), (None, 0), 'nothing is claimed for the old prefix')
        self.assertEqual(list(stamper.pending_commitments), [commitment_of(self.proofs[3])],
                         'what the database holds is skipped, what it lacks is pending')
        anchor(stamper, 200)
        self.assertTrue(upgrade(self.proofs[3], cal))
        close(cal)
        self.assertEqual(read_checkpoint(self.known_good), (4, generation), 'the next anchor writes the current form')
        with self.assertNoLogs(level='WARNING'):
            cal, stamper = self.started(self.box)
        self.assertEqual((cal.checkpoint, cal.generation), (4, generation), 'and the step is not taken again')
        close(cal)

    def test_a_stop_on_either_side_of_the_adoption_converges_on_one_generation(self):
        os.unlink(self.known_good)
        real = otsserver.calendar.LevelDbCalendar.adopt_generation
        fired = []

        def stop_before(db, watermark):
            fired.append('before')
            raise Stop()

        def stop_after(db, watermark):
            real(db, watermark)
            fired.append('after')
            raise Stop()
        for stopper in (stop_before, stop_before, stop_after):
            with mock.patch.object(otsserver.calendar.LevelDbCalendar, 'adopt_generation', stopper), \
                    self.assertRaises(Stop):
                Calendar(os.path.join(self.box, 'calendar'))
            gc.collect()
        generation, watermark = self.meta()
        self.assertEqual(watermark, b'0')
        with mock.patch.object(otsserver.calendar.LevelDbCalendar, 'adopt_generation', stop_after):
            cal, stamper = self.started(self.box)       # not stopped: there is a generation, and nothing to adopt
        self.assertEqual(fired, ['before', 'before', 'after'], 'the batch landed once, at the third start')
        self.assertEqual(cal.generation, generation.decode())
        self.conserved(self.box, cal, stamper)
        close(cal)


class Test_a_recovery_that_is_stopped_again(CalendarCase):
    """R6 for the calendar's recoveries: the settling of a marker found at
    a start, from each state a stop or a restore can leave it in, and the
    publication of the checkpoint. Fault model: faults.Stop at the n-th
    write, truncate, fsync, unlink, rename or replace, n from 1 to as many
    as a clean recovery of that fixture makes; every case from a fresh
    copy of the fixture, every injection asserted to have fired, every
    recovery stopped a second time at its first remaining call of the same
    kind before it is left to finish."""

    FAMILIES = ('write', 'ftruncate', 'fsync', 'unlink', 'rename', 'replace')

    def setUp(self):
        super().setUp()
        self.story()

    def settle(self, box):
        """The settling the stamper's open does, called here in the test's
        own thread so that a stop surfaces in it."""
        cal = self.open(box)
        try:
            stamper = make_stamper(receipts_path(box))
            stamper.calendar = cal
            stamper.settle_pending_receipts()
        finally:
            close(cal)

    def fixtures(self):
        """name -> (the box, the receipts a finished recovery leaves)"""
        a, b = self.txids['A'], self.txids['B']
        boxes = {'the save did not happen': (self.compose('f-marker', 'B-marker'), [a]),
                 'saved, the receipt owed': (self.compose('f-saved', 'B-saved'), [a, b]),
                 'receipted, the marker standing': (self.compose('f-receipted', 'B-receipted'), [a, b])}
        # The append was stopped part way: a line without its newline.
        torn = self.compose('f-torn', 'B-saved')
        with open(receipts_path(torn), 'ab') as fd:
            fd.write(b'{"txid": "%s", "fee_sa' % b.encode())
        boxes['saved, the append torn'] = (torn, [a, b])
        # The single marker name of earlier releases (R4): still settled, once.
        old = self.compose('f-old-name', 'B-saved')
        receipts = receipts_path(old)
        os.rename(marker_path(receipts, b), marker_path(receipts))
        boxes['saved, the marker under its old name'] = (old, [a, b])
        # A marker that does not parse is set aside, and nothing is guessed.
        bad = self.compose('f-unreadable', 'B-saved')
        with open(marker_path(receipts_path(bad), b), 'w') as fd:
            fd.write('{"receipt": ')
        boxes['a marker that does not parse'] = (bad, [a])
        return boxes

    def fresh(self, box, tag):
        copy = '%s-%s' % (box, tag)
        shutil.copytree(box, copy)
        return copy

    def calls(self, box, family, tag):
        with mock.patch.object(otsserver.stamper.os, family, wraps=getattr(os, family)) as counted, \
                self.assertLogs(level='INFO'):
            self.settle(self.fresh(box, tag))
        return counted.call_count

    def test_every_write_of_every_settling(self):
        swept = {}
        for name, (box, receipts) in self.fixtures().items():
            for family in self.FAMILIES:
                total = self.calls(box, family, 'count-' + family)
                swept[name, family] = total
                for n in range(1, total + 1):
                    with self.subTest(fixture=name, family=family, n=n):
                        case = self.fresh(box, '%s-%d' % (family, n))
                        with fail_on_call(otsserver.stamper.os, family, n, exc=Stop()) as hit:
                            with self.assertRaises(Stop):
                                self.settle(case)
                        self.assertTrue(hit['fired'])
                        if self.calls(case, family, 'probe'):
                            with fail_on_call(otsserver.stamper.os, family, 1, exc=Stop()) as again:
                                with self.assertRaises(Stop):
                                    self.settle(case)
                            self.assertTrue(again['fired'])
                        cal, stamper = self.started(case)
                        self.assertEqual(self.txids_on_file(case), receipts, 'one receipt per saved anchor')
                        self.assertEqual(markers(case), [])
                        self.conserved(case, cal, stamper)
                        close(cal)
                        cal, stamper = self.started(case)
                        self.assertEqual(self.txids_on_file(case), receipts, 'and a further start adds nothing')
                        close(cal)
        # What was swept, so that the coverage claimed is the coverage there is.
        self.assertEqual({key: n for key, n in swept.items() if n}, {
            ('the save did not happen', 'unlink'): 1,
            ('saved, the receipt owed', 'write'): 1, ('saved, the receipt owed', 'fsync'): 2,
            ('saved, the receipt owed', 'unlink'): 1,
            ('receipted, the marker standing', 'fsync'): 2, ('receipted, the marker standing', 'unlink'): 1,
            ('saved, the append torn', 'write'): 1, ('saved, the append torn', 'ftruncate'): 1,
            ('saved, the append torn', 'fsync'): 3, ('saved, the append torn', 'unlink'): 1,
            ('saved, the marker under its old name', 'write'): 1, ('saved, the marker under its old name', 'fsync'): 2,
            ('saved, the marker under its old name', 'unlink'): 1,
            ('a marker that does not parse', 'rename'): 1})

    def test_a_receipt_found_on_file_is_synced_before_its_marker_goes(self):
        """The append's fsync failed (an OSError): the line is on file, not
        known to be synced, and its marker stays. The next settling finds
        the line, never removing the marker on sight and the receipt's only
        other copy with it."""
        box = self.compose('unsynced', 'B-saved')
        receipts = receipts_path(box)
        with fail_on_call(otsserver.stamper.os, 'fsync', 1) as hit, self.assertLogs(level='WARNING') as logs:
            self.settle(box)
        self.assertTrue(hit['fired'])
        self.assertTrue(any('could not be settled' in line for line in logs.output), logs.output)
        self.assertEqual(self.txids_on_file(box), [self.txids['A'], self.txids['B']])
        self.assertEqual(len(markers(box)), 1, 'the marker stays while the line is not known to be synced')

        order = []
        real_fsync, real_unlink, real_open = os.fsync, os.unlink, os.open
        opened = {}

        def note_open(path, *args, **kwargs):
            fd = real_open(path, *args, **kwargs)
            opened[fd] = os.path.basename(path)
            return fd

        def note_fsync(fd):
            order.append(('fsync', opened.get(fd)))
            return real_fsync(fd)

        def note_unlink(path, *args, **kwargs):
            order.append(('unlink', os.path.basename(path)))
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(otsserver.stamper.os, 'open', note_open), \
                mock.patch.object(otsserver.stamper.os, 'fsync', note_fsync), \
                mock.patch.object(otsserver.stamper.os, 'unlink', note_unlink), self.assertLogs(level='INFO'):
            self.settle(box)
        self.assertEqual(order, [('fsync', 'anchor-receipts.jsonl'), ('fsync', 'receipts'),
                                 ('unlink', 'anchor-receipts.jsonl.pending.' + self.txids['B'])])
        self.assertEqual(self.txids_on_file(box), [self.txids['A'], self.txids['B']])

    def test_a_discard_the_next_anchor_cannot_turn_into_a_receipt(self):
        """On the real store: B's marker stands
        and B's save never happened; the discard's unlink is refused, an
        OSError the settling handles: the marker is reported and stays.
        The same commitments go out again in C, which saves. B's marker is
        asked about at C's save and again afterwards, and B is never owed:
        the calendar is asked for B's own txid node, which C's tree does
        not carry. (Before this the question was a commitment of B's tree,
        which C's save answered for: five records accepted, seven
        receipted.) Fault model: a handled OSError at one unlink, and
        progress past it."""
        box = self.compose('unlink-refused', 'B-marker')
        marker_b, real_unlink, refused = marker_path(receipts_path(box), self.txids['B']), os.unlink, []

        def refuse_b(path, *args, **kwargs):
            if path == marker_b:
                refused.append(path)
                raise PermissionError(13, 'Permission denied', path)
            return real_unlink(path, *args, **kwargs)
        with mock.patch.object(otsserver.stamper.os, 'unlink', refuse_b), self.assertLogs(level='WARNING') as logs:
            cal, stamper = self.started(box)
            self.assertNotIn(self.commitments[2], cal)
            anchor(stamper, 300)                          # C, over entries 2, 3 and 4
        self.assertEqual(len(refused), 2, 'asked about at the start and again before the next marker')
        self.assertTrue(any('could not be settled' in line and 'PermissionError' in line for line in logs.output),
                        logs.output)
        self.assertIn(self.commitments[2], cal)
        lines, _ = receipt_lines(box)
        self.assertEqual(([line['txid'] for line in lines][0], len(lines)), (self.txids['A'], 2), 'A and C')
        self.assertNotIn(self.txids['B'], [line['txid'] for line in lines])
        self.assertTrue(os.path.exists(marker_b), 'retried, not forgotten')
        with self.assertLogs(level='WARNING') as logs:
            stamper.settle_pending_receipts()              # the unlink works again
        self.assertEqual(receipt_lines(box)[0], lines, 'B is never owed')
        self.assertEqual(markers(box), [])
        self.assertTrue(any('discarded' in line and self.txids['B'] in line for line in logs.output), logs.output)
        self.assertEqual(sum(line['records'] for line in lines), 5, 'five accepted, five receipted')
        close(cal)

    def test_every_write_of_the_checkpoint(self):
        """B is saved and receipted; its checkpoint (4) is being published
        over the one on file (2). A stop anywhere leaves 2 or 4, both at or
        below the watermark, and either starts."""
        box = self.compose('f-checkpoint', 'B-unmarked')
        known_good = os.path.join('calendar', 'journal.known-good')
        generation = read_checkpoint(os.path.join(box, known_good))[1]
        swept = {}
        for family in ('write', 'fsync', 'replace'):
            with mock.patch.object(otsserver.calendar.os, family, wraps=getattr(os, family)) as counted:
                write_checkpoint(os.path.join(self.fresh(box, 'count-' + family), known_good), 4, generation)
            swept[family] = counted.call_count
            for n in range(1, counted.call_count + 1):
                with self.subTest(family=family, n=n):
                    case = self.fresh(box, '%s-%d' % (family, n))
                    for attempt in (n, 1):      # the stop, then the republication stopped at its first such call
                        with fail_on_call(otsserver.calendar.os, family, attempt, exc=Stop()) as hit:
                            with self.assertRaises(Stop):
                                write_checkpoint(os.path.join(case, known_good), 4, generation)
                        self.assertTrue(hit['fired'])
                        cal, stamper = self.started(case)
                        self.assertIn(cal.checkpoint, (2, 4))
                        self.conserved(case, cal, stamper)
                        close(cal)
                    write_checkpoint(os.path.join(case, known_good), 4, generation)
                    cal, stamper = self.started(case)
                    self.assertEqual(cal.checkpoint, 4)
                    close(cal)
        self.assertEqual(swept, {'write': 0, 'fsync': 2, 'replace': 1})


class Test_messages_name_no_path(CalendarCase):
    """No message of a restore, a migration or a recovery names the
    directory the calendar or its receipts are in: the path is the
    operator's and can name a client. Every box here is under a directory
    named PRIVATE, and every record logged, at any level, is read. A
    traceback, which Python
    prints for an error nothing expected, names source files and is not
    one of these messages."""

    def setUp(self):
        super().setUp()
        self.story()

    def logged(self, action):
        with self.assertLogs(level='DEBUG') as logs:
            try:
                result = action()
            except SystemExit:
                result = None
                gc.collect()
        if isinstance(result, tuple):
            close(result[0])
        return '\n'.join(record.getMessage() for record in logs.records)

    def test_no_message_names_the_directory(self):
        a_v1 = self.compose('v1', 'B-checkpointed')
        with open(os.path.join(a_v1, 'calendar', 'journal.known-good'), 'w') as fd:
            fd.write('4\n')
        malformed = self.compose('malformed', 'B-checkpointed')
        with open(os.path.join(malformed, 'calendar', 'journal.known-good'), 'w') as fd:
            fd.write('torn\n')
        torn_db = self.compose('torn-db', 'B-checkpointed')
        tables = [n for n in os.listdir(os.path.join(torn_db, 'calendar', 'db')) if n.endswith('.ldb')]
        os.unlink(os.path.join(torn_db, 'calendar', 'db', tables[0]))
        other_lineage = self.compose('lineage', 'B-checkpointed', db=None)
        foreign_journal = self.compose('foreign', 'B-checkpointed')
        with open(os.path.join(foreign_journal, 'calendar', 'journal'), 'r+b') as fd:
            fd.seek(3 * Journal.COMMITMENT_SIZE)        # the entry below the checkpoint
            fd.write(b'\x66' * 36 + b'\x00' * 8)
        no_uri = self.compose('no-uri', 'B-checkpointed')
        os.unlink(os.path.join(no_uri, 'calendar', 'uri'))
        torn_tail = self.compose('torn-tail', 'B-saved')
        with open(receipts_path(torn_tail), 'ab') as fd:
            fd.write(b'{"txid": "')
        bad_marker = self.compose('bad-marker', 'B-saved')
        with open(marker_path(receipts_path(bad_marker), self.txids['B']), 'w') as fd:
            fd.write('not json')
        boxes = {   # the case: (the box, words of the message the case is about)
            'a checkpoint from before generations': (a_v1, 'an index alone'),
            'a malformed checkpoint': (malformed, 'is malformed'),
            'a database that does not open': (torn_db, 'db/ does not open'),
            'a database of another lineage': (other_lineage, 'different lineage'),
            'an older database': (self.compose('older-db', 'entry-4', checkpoint='B-checkpointed'), 'older than the checkpoint'),
            'a journal short of the checkpoint': (self.compose('short', 'B-checkpointed', journal='entry-2', counts='entry-2'),
                                                  'the journal holds 3 entries'),
            'a journal of another lineage': (foreign_journal, 'does not hold journal entry 3'),
            'a sidecar that outlasts the journal': (self.compose('sidecar', 'entry-2', counts='entry-3'), 'journal.counts holds'),
            'a receipt ahead of the database': (self.compose('ahead', 'B-marker', receipts='B-receipted'), 'newer than db/'),
            'no uri': (no_uri, 'Calendar URI not yet set'),
            'a receipt owed': (self.compose('owed', 'B-saved'), 'recovered from the pending marker'),
            'a receipt not owed': (self.compose('not-owed', 'B-marker'), 'nothing is owed'),
            'a receipt already on file': (self.compose('on-file', 'B-receipted'), 'already on file'),
            'a torn receipt line': (torn_tail, 'incomplete receipt line'),
            'a marker that does not parse': (bad_marker, 'is unreadable'),
        }
        for name, (box, words) in boxes.items():
            with self.subTest(case=name):
                text = self.logged(lambda: self.start(box))
                self.assertNotIn(PRIVATE, text)
                self.assertNotIn(self.tmpdir.name, text)
                self.assertIn(words, text, 'the case must reach the message it is about')

    def stamper_alone(self, box):
        """The stamper's own start over a calendar doubled by its path: with
        a real calendar the storage check has spoken first. Returns
        (the stamper, every message logged)."""
        stop = threading.Event()
        with self.assertLogs(level='DEBUG') as logs, mock.patch.object(Stamper, '_Stamper__do_bitcoin'):
            stamper = Stamper(types.SimpleNamespace(path=os.path.join(box, 'calendar')), stop, 12, 1, 6, 600, 100000, 100)
            stamper.thread.join(10)
        return stamper, '\n'.join(record.getMessage() for record in logs.records)

    def test_the_stampers_own_words_for_a_checkpoint_it_cannot_read(self):
        box = self.compose('stamper', 'B-checkpointed')
        with open(os.path.join(box, 'calendar', 'journal.known-good'), 'w') as fd:
            fd.write('torn\n')
        os.environ.pop('OTSD_ANCHOR_RECEIPTS', None)      # and receipts off beside a sidecar: the other message
        stamper, text = self.stamper_alone(box)
        self.assertIsNotNone(stamper.failure)
        self.assertIn('journal.known-good is malformed', text)
        self.assertIn('were on before and are off now', text)
        self.assertNotIn(PRIVATE, text)

    def test_a_checkpoint_that_cannot_be_read_is_refused_by_its_class_at_both_readers(self):
        """A restore that lost the file's permissions. Both readers refuse
        with the recovery, naming the error's class and errno, never the
        path: no traceback out of Calendar(), no CRITICAL line with the
        path inside the error's text. Fault model: chmod 000."""
        box = self.compose('unreadable', 'B-checkpointed')
        self.addCleanup(unreadable(os.path.join(box, 'calendar', 'journal.known-good')))
        text = self.refused(box)
        self.assertIn('journal.known-good cannot be read (PermissionError errno=13)', text)
        self.assertNotIn(PRIVATE, text)
        stamper, text = self.stamper_alone(box)
        self.assertIsNotNone(stamper.failure)
        self.assertIn('journal.known-good cannot be read (PermissionError errno=13)', text)
        self.assertIn('Recovery', text)
        self.assertNotIn(PRIVATE, text)

    def test_a_malformed_checkpoint_is_described_not_quoted(self):
        """The refusal must not echo what the file held, and a digit int()
        does not take (`²` passes str.isdigit) must not make int() quote
        the field at either reader. Neither reader passes an exception's
        text on."""
        cases = {  # what the file holds: what both readers say of it
            b'synthetic-private-text-copied-to-the-wrong-file\n': 'the first field is not a decimal number of at most 20 digits (47 characters)',
            b'one two three\n': 'not "INDEX" or "INDEX GENERATION": 14 bytes, 3 fields',
            '4400123456789\u00b2\n'.encode(): 'not ASCII text (16 bytes)',
            b'4400123456789x ab\n': 'the first field is not a decimal number of at most 20 digits (14 characters)',
            b'ACME LAB PRIVATE CHECKPOINT CONTENT\xff': 'not ASCII text (36 bytes)',
        }
        for content, words in cases.items():
            with self.subTest(content=content):
                box = self.compose('quoted-%d' % len(content), 'B-checkpointed')
                with open(os.path.join(box, 'calendar', 'journal.known-good'), 'wb') as fd:
                    fd.write(content)
                text = self.refused(box)
                stamper, said = self.stamper_alone(box)
                self.assertIsNotNone(stamper.failure)
                for private in ('synthetic-private', '4400123456789', 'ACME'):
                    self.assertNotIn(private, text)
                    self.assertNotIn(private, said)
                self.assertIn('is malformed (%s)' % words, text, 'the case must reach the message it is about')
                self.assertIn('is malformed (%s)' % words, said)

    def test_a_marker_that_does_not_parse_is_named_by_role_and_error_class_only(self):
        """A marker of bytes that are not UTF-8 must not be logged with the
        decoding error's repr, which carries the bytes; and a receipts
        file whose own name holds `.pending.` followed by 56 characters
        gives its single-name marker a 64-character suffix that must not
        be printed as an anchor. The class alone, and an anchor only when
        the suffix is a txid."""
        private = b'ACME LAB PRIVATE CONTENT'
        box = self.compose('binary-marker', 'B-saved')
        with open(marker_path(receipts_path(box), self.txids['B']), 'wb') as fd:
            fd.write(private + b'\xff')
        text = self.logged(lambda: self.start(box))
        self.assertNotIn(private.decode(), text)
        self.assertNotIn('ACME', text)
        self.assertIn('the pending receipt marker of anchor %s is unreadable (UnicodeDecodeError)' % self.txids['B'], text)

        fragment = 'ACME-LAB-PRIVATE' + 'x' * 40
        name = 'receipts.pending.' + fragment          # 56 characters after `.pending.`
        with mock.patch(__name__ + '.RECEIPTS', name):
            box = self.compose('odd-name', 'B-saved')
            directory = os.path.join(box, 'receipts')
            for entry in os.listdir(directory):
                os.unlink(os.path.join(directory, entry))
            with open(receipts_path(box), 'w') as fd:
                fd.write('')
            with open(marker_path(receipts_path(box)), 'w') as fd:   # the single old name
                fd.write('not json')
            text = self.logged(lambda: self.start(box))
            self.assertNotIn('ACME', text)
            self.assertNotIn(fragment, text)
            self.assertIn('the pending receipt marker under the old single name is unreadable (JSONDecodeError)', text)

    def test_the_receipts_files_name_is_in_no_message(self):
        """OTSD_ANCHOR_RECEIPTS is the operator's choice, and its name can say
        whose calendar this is. Every message about a marker names its
        role and its anchor's txid instead, never its file name."""
        name, default = 'Acme-lab-receipts.jsonl', RECEIPTS

        def renamed(box):
            directory = os.path.join(box, 'receipts')
            for entry in os.listdir(directory):
                if entry.startswith(default):
                    os.rename(os.path.join(directory, entry), os.path.join(directory, name + entry[len(default):]))
            return box

        def refusing_unlink(box):
            marker = marker_path(receipts_path(box), self.txids['B'])
            real = os.unlink

            def refuse(path, *args, **kwargs):
                if path == marker:
                    raise PermissionError(13, 'Permission denied', path)
                return real(path, *args, **kwargs)
            with mock.patch.object(otsserver.stamper.os, 'unlink', refuse):
                return self.start(box)
        cases = {
            'a receipt owed': ('B-saved', {}, 'recovered from the pending marker', self.start),
            'a receipt not owed': ('B-marker', {}, 'nothing is owed', self.start),
            'a receipt already on file': ('B-receipted', {}, 'already on file', self.start),
            'a receipt ahead of the database': ('B-marker', {'receipts': 'B-receipted'}, 'newer than db/', self.start),
            'a marker that cannot be removed': ('B-marker', {}, 'could not be settled', refusing_unlink),
        }
        for case, (point, members, words, run) in cases.items():
            with self.subTest(case=case), mock.patch(__name__ + '.RECEIPTS', name):
                box = renamed(self.compose('named-' + case.replace(' ', '-'), point, **members))
                if case == 'a receipt not owed':
                    with open(receipts_path(box), 'ab') as fd:
                        fd.write(b'{"txid": "')                              # and a torn line, for its message
                text = self.logged(lambda: run(box))
                self.assertNotIn(name, text)
                self.assertNotIn('Acme-lab', text)
                self.assertIn(words, text, 'the case must reach the message it is about')
        with mock.patch(__name__ + '.RECEIPTS', name):
            box = renamed(self.compose('named-unreadable-marker', 'B-saved'))
            with open(marker_path(receipts_path(box), self.txids['B']), 'w') as fd:
                fd.write('not json')
            text = self.logged(lambda: self.start(box))
            self.assertNotIn('Acme-lab', text)
            self.assertIn('the pending receipt marker of anchor %s is unreadable' % self.txids['B'], text)
            self.assertIn('set aside beside the receipts file with the suffix .corrupt-', text)


if __name__ == '__main__':
    unittest.main()
