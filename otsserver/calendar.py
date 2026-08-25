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
import leveldb
import logging
import os
import queue
import struct
import sys
import threading
import time

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
        commitment (its merkle tree's leaf count); None means unknown.
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

        if self.record_counts is not None and records:
            try:
                self.record_counts.put(idx, records)
            except Exception as exp:
                # The commitment is already durable; a lost count only
                # undercounts the eventual receipt, the acceptable failure
                # direction. It must never break aggregation.
                logging.warning("Failed to write record count for journal entry %d: %r" % (idx, exp))


class RecordCounts:
    """Read-only accessor for the journal's record-count sidecar

    The sidecar (journal path + '.counts') holds one 4-byte big-endian
    integer per journal entry index: the number of digest submissions
    (merkle tree leaves) aggregated under that entry. Holes — entries from
    before OTSD_ANCHOR_RECEIPTS was set, or counts lost to a crash — read
    as zero, and a real tree always has at least one leaf, so zero means
    "unknown" and is returned as None. Callers sum None as 0: receipts may
    only ever undercount.
    """
    RECORD_SIZE = 4

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
                logging.warning("Cannot read record-count sidecar %s: %r; "
                                "counts unknown (summed as 0) until it reads again"
                                % (self.path, exp))
                self.io_failed = True
            return None

        if self.io_failed:
            self.io_failed = False
            logging.info("Record-count sidecar %s is readable again" % self.path)

        if len(data) != self.RECORD_SIZE:
            return None

        count = struct.unpack('>L', data)[0]
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

class LevelDbCalendar:
    def __init__(self, path):
        self.db = leveldb.LevelDB(path)

    def __contains__(self, msg):
        try:
            self.db.Get(msg)
            return True
        except KeyError:
            return False

    def __get_timestamp(self, msg):
        """Get a timestamp, non-recursively"""
        serialized_timestamp = self.db.Get(msg)
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

        batch.Put(new_timestamp.msg, ctx.getbytes())
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

    def add_timestamps(self, new_timestamps):
        batch = leveldb.WriteBatch()
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

        self.db.Write(batch, sync=True)
        logging.debug("Done LevelDbCalendar.add_timestamps(), added %d timestamps total" % n)

class Calendar:
    def __init__(self, path):
        path = os.path.normpath(path)
        os.makedirs(path, exist_ok=True)
        self.path = path
        self.journal = JournalWriter(path + '/journal')

        self.db = LevelDbCalendar(path + '/db')

        try:
            uri_path = self.path + '/uri'
            with open(uri_path, 'r') as fd:
                self.uri = fd.read().strip()
        except FileNotFoundError as err:
            logging.error('Calendar URI not yet set; %r does not exist' % uri_path)
            sys.exit(1)

        try:
            hmac_key_path = self.path + '/hmac-key'
            with open(hmac_key_path, 'rb') as fd:
                self.hmac_key = fd.read()
        except FileNotFoundError as err:
            logging.error('HMAC secret key not set; %r does not exist' % hmac_key_path)
            sys.exit(1)

    def submit(self, submitted_commitment, records=None):
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

    def add_commitment_timestamps(self, new_timestamps):
        """Add timestamps"""
        self.db.add_timestamps(new_timestamps)


class Aggregator:
    def __loop(self):
        logging.info("Starting aggregator loop")
        while not self.exit_event.wait(self.commitment_interval):
            digests = []
            done_events = []
            last_commitment = time.time()
            while not self.digest_queue.empty():
                # This should never raise the Empty exception, as we should be
                # the only thread taking items off the queue
                (digest, done_event) = self.digest_queue.get_nowait()
                digests.append(digest)
                done_events.append(done_event)

            if not len(digests):
                continue

            digests_commitment = make_merkle_tree(digests)

            logging.info("Aggregated %d digests under commitment %s" % (len(digests), b2x(digests_commitment.msg)))

            self.calendar.submit(digests_commitment, records=len(digests))

            # Notify all requesters that the commitment is done
            for done_event in done_events:
                done_event.set()

    def __init__(self, calendar, exit_event, commitment_interval=1,
                 dedupe_horizon=3600, dedupe_max_entries=65536):
        self.calendar = calendar
        self.commitment_interval = commitment_interval
        self.digest_queue = queue.Queue()
        self.exit_event = exit_event

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

    def submit(self, msg):
        """Submit message for aggregation

        Aggregator thread will aggregate the message along with all other
        messages, and return a Timestamp

        A msg identical to one submitted within the last dedupe_horizon
        seconds does not become a second pending commitment: the caller is
        attached to the earlier submission's timestamp — same receipt path,
        counted as a record once. The dedupe keys on msg exactly as
        submitted, before the nonce below is applied.
        """
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
                self.digest_queue.put((nonce_timestamp(timestamp), done_event))

        done_event.wait()

        return timestamp
