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

import hashlib
import logging
import os
import queue
import secrets
import struct
import sys
import threading
import time

import plyvel

from opentimestamps.core.notary import TimeAttestation, PendingAttestation, BitcoinBlockHeaderAttestation
from opentimestamps.core.op import Op, OpPrepend, OpAppend, OpSHA256
from opentimestamps.core.serialize import BytesDeserializationContext, BytesSerializationContext, StreamSerializationContext, StreamDeserializationContext, DeserializationError
from opentimestamps.core.timestamp import Timestamp, make_merkle_tree
from opentimestamps.timestamp import nonce_timestamp

from bitcoin.core import b2x, b2lx

# If you can make 64-bit hash collisions we'll let you add your junk to our
# calendar.
HMAC_SIZE = 8

def derive_key_for_idx(key, idx, bits=32):
    """Derive key for an index

    Uses a binary tree so that parts of the tree can be efficiently revealed
    later.
    """
    if not bits:
        return key
    else:
        key += b'\xff' if (idx >> bits-1) & 0b1 else b'\x00'
        hashed_key = hashlib.sha256(key).digest()
        return derive_key_for_idx(hashed_key, idx, bits - 1)

class Journal:
    """Append-only commitment storage

    The journal exists simply to make sure we never lose a commitment.
    """
    COMMITMENT_SIZE = 4 + 32 + HMAC_SIZE

    def __init__(self, path):
        self.read_fd = open(path, "rb")

    def __getitem__(self, idx):
        self.read_fd.seek(idx * self.COMMITMENT_SIZE)
        commitment = self.read_fd.read(self.COMMITMENT_SIZE)

        if len(commitment) == self.COMMITMENT_SIZE:
            # Strip off HMAC if not present
            if commitment[-HMAC_SIZE:] == b'\x00'*HMAC_SIZE:
                commitment = commitment[:-HMAC_SIZE]
            return commitment
        else:
            raise KeyError()


class JournalWriter(Journal):
    """Writer for the journal"""
    def __init__(self, path):
        self.append_fd = open(path, "ab")

        # In case a previous write partially failed, seek to a multiple of the
        # commitment size
        logging.info("Opening journal for appending...")
        pos = self.append_fd.tell()

        if pos % self.COMMITMENT_SIZE:
            logging.error("Journal size not a multiple of commitment size; %d bytes excess; writing padding" % (pos % self.COMMITMENT_SIZE))
            self.append_fd.write(b'\x00'*(self.COMMITMENT_SIZE - (pos % self.COMMITMENT_SIZE)))

        logging.info("Journal has %d entries" % (self.append_fd.tell() // self.COMMITMENT_SIZE))

        # Record-count sidecar for anchor receipts, written only after the
        # journal entry itself is durable so a crash can lose counts but
        # never invent them. Created only when the anchor-receipts feature
        # is on; unset, nothing here changes.
        self.record_counts = RecordCountsWriter(path + '.counts') \
            if os.getenv("OTSD_ANCHOR_RECEIPTS") else None

    def submit(self, commitment, records=None):
        """Add a new commitment to the journal

        Returns only after the commitment is synchronized to disk.

        records is the number of digest submissions aggregated under this
        commitment (its merkle tree's leaf count); None means unknown; 0
        means known to hold no billable record (an operator-lane-only tree,
        see Aggregator.submit) and is stored as the KNOWN_ZERO sentinel.
        """
        # Pad with null HMAC if necessary
        if len(commitment) == self.COMMITMENT_SIZE - HMAC_SIZE:
            commitment += b'\x00'*HMAC_SIZE

        elif len(commitment) != self.COMMITMENT_SIZE:
            raise ValueError("Journal commitments must be exactly %d bytes long" % self.COMMITMENT_SIZE)

        assert (self.append_fd.tell() % self.COMMITMENT_SIZE) == 0
        idx = self.append_fd.tell() // self.COMMITMENT_SIZE
        self.append_fd.write(commitment)
        self.append_fd.flush()
        os.fsync(self.append_fd.fileno())

        if self.record_counts is not None and records is not None:
            try:
                self.record_counts.put(idx, records or RecordCounts.KNOWN_ZERO)
            except Exception as exp:
                # The commitment is already durable; a lost count only
                # undercounts the eventual receipt, the acceptable failure
                # direction. It must never break aggregation.
                logging.warning("Failed to write record count for journal entry %d: %r" % (idx, exp))


def read_checkpoint(path):
    """journal.known-good: None when absent, else (journal index, database
    generation as 32 hex chars, or None for a file from before generations).

    A v1 file (upstream's form) holds the index alone; a v2 file holds
    'INDEX GENERATION'. A v1 file is read only so that the calendar can
    say what it found when it refuses it
    (Calendar.verify_checkpoint). Anything else raises ValueError:
    a malformed checkpoint is refused, never guessed at, and the caller
    stops the whole service. The message says the shape of what was
    found, never its bytes, and no exception's own text is passed on: an
    int() that is refused quotes what it was given. A file that cannot be
    read for any reason but absence raises the OSError: the callers
    refuse on it with the recovery, by its class and errno.
    """
    try:
        with open(path, 'rb') as fd:
            raw = fd.read()
    except FileNotFoundError:
        return None
    try:
        text = raw.decode('ascii')
    except UnicodeDecodeError:
        raise ValueError('not ASCII text (%d bytes)' % len(raw))
    parts = text.split()
    if not parts or len(parts) > 2:
        raise ValueError('not "INDEX" or "INDEX GENERATION": %d bytes, %d field%s'
                         % (len(raw), len(parts), '' if len(parts) == 1 else 's'))
    # The digits themselves, not str.isdigit(), which also takes digits
    # int() does not and whose ValueError then quotes the field.
    if not parts[0] or len(parts[0]) > 20 or any(c not in '0123456789' for c in parts[0]):
        raise ValueError('the first field is not a decimal number of at most 20 digits (%d characters)' % len(parts[0]))
    generation = None
    if len(parts) == 2:
        generation = parts[1].lower()
        if len(generation) != 32 or any(c not in '0123456789abcdef' for c in generation):
            raise ValueError('the second field is not a generation of 32 hex characters (%d characters)' % len(generation))
    return int(parts[0]), generation


def error_text(exp):
    """An OSError as a message records it: the class and the errno, never
    its text, which names the path."""
    text = type(exp).__name__
    if getattr(exp, 'errno', None):
        text += ' errno=%d' % exp.errno
    return text


def fsync_dir(path):
    """fsync the directory holding path, so a rename or a new entry is durable"""
    fd = os.open(os.path.dirname(os.path.abspath(path)) or '.', os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_checkpoint(path, idx, generation):
    """Write journal.known-good atomically: tmp, fsync, rename, fsync dir.
    Always 'INDEX GENERATION': an index without the generation of the
    database it describes is a file the next start refuses."""
    tmp = path + '.tmp'
    with open(tmp, 'w') as fd:
        fd.write('%d %s\n' % (idx, generation))
        fd.flush()
        os.fsync(fd.fileno())
    os.replace(tmp, path)
    fsync_dir(path)


class RecordCounts:
    """Read-only accessor for the journal's record-count sidecar

    The sidecar (journal path + '.counts') holds one 4-byte big-endian
    integer per journal entry index: the number of digest submissions
    (merkle tree leaves) aggregated under that entry. Holes — entries from
    before OTSD_ANCHOR_RECEIPTS was set, or counts lost to a crash — read
    as zero, and a real tree always has at least one leaf, so zero means
    "unknown" and is returned as None. Callers sum None as 0: receipts may
    only ever undercount. A known zero — a tree whose leaves were all
    operator-lane submissions — is stored as KNOWN_ZERO and returned as 0,
    which is a fact, not a hole.
    """
    RECORD_SIZE = 4

    # Zero in the file already means unknown, so known zero is stored as
    # all-ones. Any value >= KNOWN_ZERO_THRESHOLD reads as 0: a one-second
    # tree cannot have 2**31 leaves, and every torn big-endian prefix of the
    # sentinel (0xFF000000, 0xFFFF0000, 0xFFFFFF00) stays above the
    # threshold, so a torn write still cannot overcount; the all-zero
    # prefix reads as unknown, summed as 0.
    KNOWN_ZERO = 0xFFFFFFFF
    KNOWN_ZERO_THRESHOLD = 0x80000000

    # True once a read failure has been warned about; class-level default
    # so the writer subclass, which does not call this __init__, shares it.
    io_failed = False

    def __init__(self, path):
        self.path = path
        self.fd = None

    def get(self, idx):
        """Return the record count for journal entry idx, or None if unknown

        Never raises: counting must never break aggregation, stamping, or
        anchoring. Any read failure is an unknown count (None, summed as
        0 — errs low), warned once until a read succeeds again.
        """
        try:
            if self.fd is None:
                try:
                    self.fd = os.open(self.path, os.O_RDONLY)
                except FileNotFoundError:
                    return None
            data = os.pread(self.fd, self.RECORD_SIZE, idx * self.RECORD_SIZE)
        except OSError as exp:
            # Drop the fd so a repaired file is picked up by reopening.
            if self.fd is not None:
                try:
                    os.close(self.fd)
                except OSError:
                    pass
                self.fd = None
            if not self.io_failed:
                logging.warning("Cannot read the record-count sidecar journal.counts: %r; "
                                "counts unknown (summed as 0) until it reads again" % (exp,))
                self.io_failed = True
            return None

        if self.io_failed:
            self.io_failed = False
            logging.info("The record-count sidecar journal.counts is readable again")

        if len(data) != self.RECORD_SIZE:
            return None

        count = struct.unpack('>L', data)[0]
        if count >= self.KNOWN_ZERO_THRESHOLD:
            return 0
        return count if count else None


class RecordCountsWriter(RecordCounts):
    """Writer for the record-count sidecar"""
    def __init__(self, path):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)

    def put(self, idx, count):
        """Record the count for journal entry idx; entries are write-once

        A torn write over the sparse file's zeros leaves a big-endian
        prefix with a zero tail, which is always <= the true value: even a
        crash mid-write can only undercount.
        """
        os.pwrite(self.fd, struct.pack('>L', count), idx * self.RECORD_SIZE)
        os.fsync(self.fd)

# The database's own identity, under keys no commitment path can produce
# (every stored msg is a 32-byte hash, a 44-byte journal commitment, a
# 64-byte merkle pair or a transaction-shaped string; these are 15 and 16
# bytes and start with a NUL). generation: 16 random bytes in hex, chosen
# when the database is created, so a recreated or restored database is a
# different one. watermark: the journal index below which every commitment
# is in this database, written in the same synchronous batch as the
# confirmed timestamps that make it true.
META_PREFIX = b'\x00meta/'
META_GENERATION = META_PREFIX + b'generation'
META_WATERMARK = META_PREFIX + b'watermark'


class LevelDbCalendar:
    # plyvel, not py-leveldb: py-leveldb's last release (0.201) bundles
    # LevelDB 1.19 statically and does not compile past Python 3.11.
    # plyvel links the system libleveldb; the on-disk format is LevelDB's
    # either way, so a calendar written under py-leveldb opens here
    # unchanged (README "Unit tests"). The API differences are all in
    # this class.
    def __init__(self, path):
        self.db = plyvel.DB(path, create_if_missing=True)
        raw = self.db.get(META_GENERATION)
        if raw is not None:
            self.generation = raw.decode()
            watermark = self.db.get(META_WATERMARK)
            self.watermark = int(watermark) if watermark else 0
        elif self.__is_empty():
            # A database created just now: give it its identity before
            # anything else lands in it.
            self.generation = None
            self.adopt_generation(0)
        else:
            # A database from before generations. It gets its generation,
            # with watermark 0, only when no checkpoint is on file and the
            # scan therefore starts at 0 (Calendar.verify_checkpoint).
            self.generation = None
            self.watermark = None

    def __is_empty(self):
        it = self.db.iterator(include_value=False)
        try:
            for _ in it:
                return False
            return True
        finally:
            it.close()

    def adopt_generation(self, watermark):
        """Stamp the database with a fresh generation and the given watermark,
        in one synchronous batch: from here on the pair (generation,
        watermark) is what a checkpoint file must agree with."""
        generation = secrets.token_hex(16)
        batch = self.db.write_batch(sync=True)
        batch.put(META_GENERATION, generation.encode())
        batch.put(META_WATERMARK, str(watermark).encode())
        batch.write()
        self.generation = generation
        self.watermark = watermark

    def __contains__(self, msg):
        if msg.startswith(META_PREFIX):
            return False
        return self.db.get(msg) is not None

    def __get_timestamp(self, msg):
        """Get a timestamp, non-recursively"""
        serialized_timestamp = self.db.get(msg) if not msg.startswith(META_PREFIX) else None
        if serialized_timestamp is None:
            raise KeyError(msg)
        ctx = BytesDeserializationContext(serialized_timestamp)

        timestamp = Timestamp(msg)

        for i in range(ctx.read_varuint()):
            attestation = TimeAttestation.deserialize(ctx)
            assert attestation not in timestamp.attestations
            timestamp.attestations.add(attestation)

        for i in range(ctx.read_varuint()):
            op = Op.deserialize(ctx)
            assert op not in timestamp.ops
            timestamp.ops.add(op)

        return timestamp

    def __put_timestamp(self, new_timestamp, batch, batch_cache):
        """Write a single timestamp, non-recursively"""
        ctx = BytesSerializationContext()

        ctx.write_varuint(len(new_timestamp.attestations))
        for attestation in new_timestamp.attestations:
            attestation.serialize(ctx)

        ctx.write_varuint(len(new_timestamp.ops))
        for op in new_timestamp.ops:
            op.serialize(ctx)

        batch.put(new_timestamp.msg, ctx.getbytes())
        batch_cache[new_timestamp.msg] = new_timestamp

    def __getitem__(self, msg):
        """Get the timestamp for a given message"""
        timestamp = self.__get_timestamp(msg)

        for op, op_stamp in timestamp.ops.items():
            timestamp.ops[op] = self[op_stamp.msg]

        return timestamp

    def __add_timestamp(self, new_timestamp, batch, batch_cache):
        existing_timestamp = None
        try:
            if new_timestamp.msg in batch_cache:
                existing_timestamp = batch_cache[new_timestamp.msg]
            else:
                existing_timestamp = self.__get_timestamp(new_timestamp.msg)

        except KeyError:
            existing_timestamp = Timestamp(new_timestamp.msg)

        else:
            if existing_timestamp == new_timestamp:
                # Note how because we didn't get the existing timestamp
                # recursively, the only way old and new can be identical is if all
                # the ops are verify operations.
                return

        # Update the existing timestamps attestations with those from the new
        # timestamp
        existing_timestamp.attestations.update(new_timestamp.attestations)

        for new_op, new_op_stamp in new_timestamp.ops.items():
            # Make sure the existing timestamp has this operation
            existing_timestamp.ops.add(new_op)

            # Add the results timestamp to the calendar
            self.__add_timestamp(new_op_stamp, batch, batch_cache)

        self.__put_timestamp(existing_timestamp, batch, batch_cache)

    def add_timestamps(self, new_timestamps, watermark=None):
        """Write the timestamps in one synchronous batch; with a watermark,
        the journal index below which everything is now in the database
        lands in the same batch, so it is exactly as durable as they are."""
        batch = self.db.write_batch(sync=True)
        batch_cache = {}

        last = time.time()
        n = 0
        for new_timestamp in new_timestamps:
            self.__add_timestamp(new_timestamp, batch, batch_cache)
            n += 1

            if n % 10000 == 0:
                now = time.time()
                logging.debug("Added %d timestamps to LevelDB; %f stamps/second" %
                              (n, 10000.0 / (now - last)))
                last = now
        del batch_cache

        if watermark is not None:
            batch.put(META_WATERMARK, str(watermark).encode())
        batch.write()
        if watermark is not None:
            self.watermark = watermark
        logging.debug("Done LevelDbCalendar.add_timestamps(), added %d timestamps total" % n)

class Calendar:
    def __init__(self, path):
        path = os.path.normpath(path)
        os.makedirs(path, exist_ok=True)
        self.path = path

        # Messages name the calendar's files by their fixed names and never
        # the directory they are in: the path is the operator's, it can
        # name a client, and the operator chose it.
        try:
            self.db = LevelDbCalendar(path + '/db')
        except plyvel.Error as exp:
            # LevelDB's own message carries the path; the class says enough.
            self.__refuse('db/ does not open (%s): another process holds it, or its files are not one database '
                          '(copied while the calendar ran, or restored in part)' % type(exp).__name__, self.DB_RECOVERY)

        try:
            with open(self.path + '/uri', 'r') as fd:
                self.uri = fd.read().strip()
        except FileNotFoundError as err:
            logging.error('Calendar URI not yet set; the file uri does not exist in the calendar directory')
            sys.exit(1)

        try:
            with open(self.path + '/hmac-key', 'rb') as fd:
                self.hmac_key = fd.read()
        except FileNotFoundError as err:
            logging.error('HMAC secret key not set; the file hmac-key does not exist in the calendar directory')
            sys.exit(1)

        # The checkpoint on file must belong to this database, lie at or
        # below what the database durably holds, and describe the journal
        # that is here; otherwise the service does not start (a lost,
        # recreated or older-restored db/ beside a kept checkpoint would
        # skip the journal entries the checkpoint claims; a lost or older
        # journal beside a kept checkpoint would take new submissions below
        # the checkpoint, where the scan never looks). Checked before the
        # journal is opened for appending, so a journal found missing is
        # never created beside a checkpoint that names entries it should
        # hold.
        self.__check_counts()
        self.checkpoint = self.verify_checkpoint()
        self.journal = JournalWriter(path + '/journal')

        # The stamper, set by otsd once both exist: its view of the chain is
        # where the not-before bound below comes from.
        self.stamper = None

    RECOVERY = ("Recovery: if db/ was restored from a backup or recreated, delete journal.known-good (in the calendar "
                "directory) and start again: the stamper rescans the whole journal from index 0 and re-anchors every "
                "commitment the database lacks. The re-anchored proofs name later blocks than the originals, whose "
                "paths lived only in the lost database. If the journal was restored or lost, restore it from the same "
                "stopped copy as db/ and the checkpoint: entries lost with a journal cannot be recovered, and deleting "
                "the checkpoint rescans the journal that is here. Never copy a journal.known-good from another database.")

    # A checkpoint from before generations is an index alone.
    RESCAN_ONCE = ("Recovery, once: delete journal.known-good (in the calendar directory) and start again. The stamper "
                   "rescans the whole journal from index 0: every commitment the database holds is skipped, every one "
                   "it lacks is anchored (later blocks than it would have had), and the next confirmed anchor writes "
                   "the checkpoint in the current form, with the database's generation.")

    DB_RECOVERY = ("Recovery: stop any other otsd on this calendar. Otherwise restore db/ from a copy taken with the "
                   "calendar stopped, together with journal, journal.counts and journal.known-good. Without such a "
                   "copy, move db/ aside and delete journal.known-good: the stamper rescans the whole journal from "
                   "index 0 and anchors every commitment again, in later blocks; proofs already complete in their "
                   "holders' hands stay valid, and the original paths in the lost database are not rebuilt.")

    UNREADABLE_RECOVERY = ("Recovery: give journal.known-good (in the calendar directory) back its permissions and "
                           "start again; or delete it: the stamper rescans the whole journal from index 0, once, and "
                           "anchors what the database lacks in later blocks.")

    COUNTS_RECOVERY = ("Recovery: restore journal and journal.counts from one copy taken with the calendar stopped; "
                       "entries lost with a journal cannot be recovered. To go on with the journal that is here, "
                       "delete journal.counts (and journal.known-good, if the start is then refused for the journal): "
                       "the counts of entries not yet receipted become unknown and their receipts undercount, never "
                       "overcount; every commitment the database lacks is anchored, in later blocks.")

    def __refuse(self, reason, recovery=None):
        logging.critical("CALENDAR STORAGE INCONSISTENT: %s. %s" % (reason, recovery or self.RECOVERY))
        sys.exit(1)

    def __check_counts(self):
        """journal.counts holds one count per journal entry, each written
        only after its entry is durable (JournalWriter.submit), so it never
        describes more entries than the journal holds. When it does, the
        journal here is older than the sidecar: entries were lost with it,
        or the two files are copies from different moments, and a count
        left by a lost entry would be read as a later entry's. One stat of
        each file; a partial last count still names its entry."""
        try:
            counted = -(-os.path.getsize(self.path + '/journal.counts') // RecordCounts.RECORD_SIZE)
        except FileNotFoundError:
            return
        try:
            entries = os.path.getsize(self.path + '/journal') // Journal.COMMITMENT_SIZE
        except FileNotFoundError:
            entries = 0
        if counted > entries:
            self.__refuse('journal.counts holds a count for journal entry %d, but the journal holds %d entr%s: the '
                          'journal is older than its sidecar (entries were lost with it, or the two files were copied '
                          'at different moments)' % (counted - 1, entries, 'y' if entries == 1 else 'ies'),
                          self.COUNTS_RECOVERY)

    @property
    def generation(self):
        return self.db.generation

    def __check_journal(self, idx):
        """A bounded check that the journal is one the checkpoint can
        describe: it holds at least idx entries, and the entry just below
        idx and entry 0 are in the database (every entry below the
        checkpoint is anchored, by the checkpoint's definition). It
        catches a missing journal, one truncated below the checkpoint,
        and one from another lineage whose entries differ at those two
        positions. It does not catch an older prefix-identical copy that
        still reaches the checkpoint (the entries beyond it are lost,
        undetectably: the snapshot rule in the README is what prevents
        that), nor a journal that differs only between the two probed
        entries. Two reads and two probes, whatever the checkpoint's
        size; the full check is the rescan from 0."""
        if idx <= 0:
            return
        journal_path = self.path + '/journal'
        try:
            entries = os.path.getsize(journal_path) // Journal.COMMITMENT_SIZE
        except FileNotFoundError:
            entries = 0
        if entries < idx:
            self.__refuse('journal.known-good names journal index %d, but the journal holds %d entr%s: the journal is '
                          'missing or older than the checkpoint (deleted, or restored from an older backup); new '
                          'submissions would land below the checkpoint and never be anchored'
                          % (idx, entries, 'y' if entries == 1 else 'ies'))
        journal = Journal(journal_path)
        try:
            for probe in (idx - 1, 0):
                try:
                    entry = journal[probe]
                except KeyError:
                    self.__refuse('journal.known-good names journal index %d, but the journal has no entry %d' % (idx, probe))
                if entry not in self.db:
                    self.__refuse('journal.known-good names journal index %d, but the database does not hold journal '
                                  'entry %d: the journal is not the one the checkpoint describes (restored from '
                                  'elsewhere?), or db/ is older than the checkpoint' % (idx, probe))
        finally:
            journal.read_fd.close()

    def verify_checkpoint(self):
        """Check journal.known-good against the database's generation and
        committed watermark, and against the journal (__check_journal);
        returns the checkpoint index the scan may start at (None: from 0),
        or stops the process with the recovery text.

        A checkpoint from before generations (an index alone) is refused,
        whatever the database holds: it does not say which database it
        describes, and a probe of one entry below it is a guess at the
        rest. The operator deletes it, once, and the scan starts at 0; a
        database without a generation gets one then, with watermark 0, in
        the one batch a new database gets its own. Adopting such a
        checkpoint when the entry below it is in the database would take
        two writes, the database then the file, and a stop between them
        would leave a start refused for the wrong reason."""
        try:
            checkpoint = read_checkpoint(self.path + '/journal.known-good')
        except ValueError as exp:
            self.__refuse('journal.known-good is malformed (%s)' % exp)
        except OSError as exp:
            self.__refuse('journal.known-good cannot be read (%s): a restore that lost its permissions, or a disk '
                          'that does not answer' % error_text(exp), self.UNREADABLE_RECOVERY)
        if checkpoint is None:
            if self.db.generation is None:
                self.db.adopt_generation(0)
                logging.info("Calendar database stamped with generation %s (no checkpoint on file; the scan starts at 0)"
                             % self.db.generation)
            return None
        idx, generation = checkpoint
        if generation is None:
            self.__refuse('journal.known-good holds an index alone (%d), the form before database generations: it '
                          'does not say which database it describes, so it is not adopted, whatever db/ holds'
                          % idx, self.RESCAN_ONCE)
        if self.db.generation is None:
            self.__refuse('journal.known-good belongs to database generation %s, but db/ carries none: db/ predates '
                          'the checkpoint (an older backup restored beside a newer checkpoint)' % generation)
        if generation != self.db.generation:
            self.__refuse('journal.known-good belongs to database generation %s, but db/ carries %s: db/ was recreated '
                          'or restored from a different lineage' % (generation, self.db.generation))
        if idx > self.db.watermark:
            self.__refuse('journal.known-good names journal index %d, but the database\'s committed watermark is %d: '
                          'db/ is older than the checkpoint (restored from an older backup?)' % (idx, self.db.watermark))
        self.__check_journal(idx)
        return idx

    # Warn once while submissions go out without a not-before bound (no
    # block known yet: startup, or bitcoind unreachable); INFO once when
    # the bound returns.
    not_before_missing_warned = False

    def best_block_hash(self):
        """The newest block the stamper has seen, in display byte order

        None until the stamper has seen a block. Display order (the hex an
        explorer shows, as bytes) so the operand in a proof can be pasted
        into any block explorer as it stands.
        """
        stamper = self.stamper
        if stamper is None:
            return None
        try:
            block_hash = stamper.known_blocks.best_block_hash()
        except Exception:
            return None
        return block_hash[::-1] if block_hash else None

    def submit(self, submitted_commitment, records=None):
        # Not-before bound: append the hash of the newest block the stamper
        # has seen, then sha256, right before the time prefix. A block hash
        # cannot be known before its block exists, so a commitment carrying
        # it provably formed after that block; the anchor's attestation
        # already says before which block. The sha256 keeps the journal
        # entry at its 44 bytes: the bound rides inside the commitment path
        # exactly as the per-submission nonce does, and every verifier that
        # follows append/sha256/prepend ops follows it unchanged. No block
        # known: no bound rather than a fabricated one, warned once.
        block_hash = self.best_block_hash()
        if block_hash is not None:
            submitted_commitment = submitted_commitment.ops.add(OpAppend(block_hash)).ops.add(OpSHA256())
            if self.not_before_missing_warned:
                self.not_before_missing_warned = False
                logging.info("not-before bound restored: commitments carry block %s" % b2lx(block_hash[::-1]))
        elif not self.not_before_missing_warned:
            self.not_before_missing_warned = True
            logging.warning("no block known yet: commitments go out without a not-before bound "
                            "until the stamper sees one")

        idx = int(time.time())

        serialized_idx = struct.pack('>L', idx)

        commitment = submitted_commitment.ops.add(OpPrepend(serialized_idx))

        per_idx_key = derive_key_for_idx(self.hmac_key, idx, bits=32)
        mac = hashlib.sha256(commitment.msg + per_idx_key).digest()[0:HMAC_SIZE]
        macced_commitment = commitment.ops.add(OpAppend(mac))

        macced_commitment.attestations.add(PendingAttestation(self.uri))
        self.journal.submit(macced_commitment.msg, records=records)

    def __contains__(self, commitment):
        return commitment in self.db

    def __getitem__(self, commitment):
        """Get commitment timestamps(s)"""
        return self.db[commitment]

    def add_commitment_timestamps(self, new_timestamps, watermark=None):
        """Add timestamps; watermark, when given, is the journal index below
        which every commitment is then in the database, committed in the
        same synchronous batch (the stamper's checkpoint after this save)."""
        self.db.add_timestamps(new_timestamps, watermark=watermark)


class AggregatorUnavailable(Exception):
    """submit() could not get its digest committed: the aggregator loop has
    stopped, or a round did not finish within submit_timeout seconds."""


class Aggregator:
    # Longest a submit() waits for its round to be committed. A round is one
    # commitment_interval plus the calendar write; anything past this means
    # the loop is wedged (a hung disk) or gone, and the caller gets
    # AggregatorUnavailable (HTTP 503 in rpc.py) instead of waiting forever.
    # Under the gateway's post timeout, so the client sees the 503 and
    # retries: the dedupe horizon attaches the retry to this same leaf.
    SUBMIT_TIMEOUT = 30

    def __loop(self):
        logging.info("Starting aggregator loop")
        while not self.exit_event.wait(self.commitment_interval):
            digests = []
            done_events = []
            records = 0
            last_commitment = time.time()
            while not self.digest_queue.empty():
                # This should never raise the Empty exception, as we should be
                # the only thread taking items off the queue
                (digest, done_event, counted) = self.digest_queue.get_nowait()
                digests.append(digest)
                done_events.append(done_event)
                if counted:
                    records += 1

            if not len(digests):
                continue

            try:
                digests_commitment = make_merkle_tree(digests)

                logging.info("Aggregated %d digests under commitment %s" % (len(digests), b2x(digests_commitment.msg)))

                # Operator-lane leaves ride the tree and its anchor but are not
                # records: they never reach a receipt or a bill. A tree of only
                # operator leaves is a known zero, not an unknown count.
                self.calendar.submit(digests_commitment, records=records)
            except Exception as exp:
                # A round that did not commit (a full disk failing the
                # journal fsync, for one) must not leave the
                # thread dead behind a live HTTP server: every later
                # submit() would wait forever and the status would still
                # show a live chain. Say so, wake the waiters with a
                # refusal, and stop the process so the supervisor restarts
                # it. What the round's clients get is a 503, no proof: the
                # commitment may or may not be in the journal (a failed
                # fsync after the write, or a partial write padded at the
                # next start), and either way it is anchored on restart
                # like any journal entry, unasked for; their resubmission
                # is a new commitment, counted again.
                logging.error("Aggregator round failed, %d digests not acknowledged (the round's commitment "
                              "may or may not be in the journal); stopping: %r" % (len(digests), exp))
                self.failure = exp
                self.exit_event.set()
                for done_event in done_events:
                    done_event.set()
                return

            # Notify all requesters that the commitment is done
            for done_event in done_events:
                done_event.set()

    def __init__(self, calendar, exit_event, commitment_interval=1,
                 dedupe_horizon=3600, dedupe_max_entries=65536):
        self.calendar = calendar
        self.commitment_interval = commitment_interval
        self.digest_queue = queue.Queue()
        self.exit_event = exit_event
        self.submit_timeout = self.SUBMIT_TIMEOUT
        # Set to the exception that stopped the loop; None while it runs.
        self.failure = None

        # Idempotency horizon: raw submitted msg -> (expires_at, timestamp,
        # done_event) for every submission still inside dedupe_horizon
        # seconds, capped at dedupe_max_entries (oldest evicted first).
        # Insertion order is expiry order — a duplicate hit re-inserts at the
        # back with a fresh expiry — so purging from the front is sufficient.
        # In-memory by design: a restart empties it, making resubmission
        # across a restart boundary the one bounded duplicate-count path.
        self.dedupe_horizon = dedupe_horizon
        self.dedupe_max_entries = dedupe_max_entries
        self._recent = {}
        self._recent_lock = threading.Lock()

        self.thread = threading.Thread(target=self.__loop)
        self.thread.start()

    def submit(self, msg, counted=True):
        """Submit message for aggregation

        Aggregator thread will aggregate the message along with all other
        messages, and return a Timestamp

        counted=False is the operator lane (rpc.py POST /operator/digest):
        the msg is aggregated and timestamped like any other leaf but is
        not a record, so it never reaches a receipt or a bill. The first
        submission of a msg decides; a dedupe hit never changes the count.

        A msg identical to one submitted within the last dedupe_horizon
        seconds does not become a second pending commitment: the caller is
        attached to the earlier submission's timestamp — same receipt path,
        counted as a record once. The dedupe keys on msg exactly as
        submitted, before the nonce below is applied.
        """
        if self.failure is not None or self.exit_event.is_set():
            raise AggregatorUnavailable('aggregator loop has stopped')

        now = time.time()

        with self._recent_lock:
            while self._recent:
                oldest = next(iter(self._recent))
                if self._recent[oldest][0] > now:
                    break
                del self._recent[oldest]

            entry = self._recent.pop(msg, None)
            if entry is not None:
                (_, timestamp, done_event) = entry
                # Refresh at the back so recurring resubmissions (e.g. a
                # client's periodic retry sweep) stay deduped beyond the
                # first pass.
                self._recent[msg] = (now + self.dedupe_horizon, timestamp, done_event)
            else:
                timestamp = Timestamp(msg)
                done_event = threading.Event()
                self._recent[msg] = (now + self.dedupe_horizon, timestamp, done_event)
                if len(self._recent) > self.dedupe_max_entries:
                    del self._recent[next(iter(self._recent))]

                # Add nonce to ensure requester doesn't learn anything about other
                # messages being committed at the same time, as well as to ensure that
                # anything we store related to this commitment can't be controlled by
                # them.
                self.digest_queue.put((nonce_timestamp(timestamp), done_event, counted))

        if not done_event.wait(self.submit_timeout):
            raise AggregatorUnavailable('aggregator round not committed within %ds' % self.submit_timeout)
        if self.failure is not None:
            raise AggregatorUnavailable('aggregator loop has stopped: %r' % (self.failure,))

        return timestamp
