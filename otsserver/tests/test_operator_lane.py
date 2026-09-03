# Copyright (C) 2026 The OpenTimestamps developers
#
# This file is part of the OpenTimestamps Server.
#
# It is subject to the license terms in the LICENSE file found in the top-level
# directory of this distribution.
#
# No part of the OpenTimestamps Server including this file, may be copied,
# modified, propagated, or distributed except according to the terms contained
# in the LICENSE file.

"""The operator lane: POST /operator/digest and the known-zero record count.

An operator-lane submission (the box's own diary, ops/selfstamp.py) is
aggregated and anchored like any client digest but is not a record: it
must never reach a receipt's "records" field, hence never a bill. Three
properties are pinned here:

- rpc: /operator/digest is a 404 unless OTSD_OPERATOR_LANE=1; when on, it
  reaches the aggregator with counted=False, while /digest stays counted.
- aggregator: a round's records = its counted leaves only; a round of
  only operator leaves reports records=0; a dedupe hit never changes the
  count (the first submission of a msg decides).
- sidecar: records=0 is a known zero, stored as the KNOWN_ZERO sentinel
  and read back as 0 (not None), so the stamper sums it silently instead
  of warning about a hole; every torn prefix of the sentinel still reads
  0, and legacy values are untouched.
"""

import hashlib
import os
import struct
import tempfile
import threading
import unittest
from io import BytesIO
from unittest import mock

from opentimestamps.core.notary import PendingAttestation
from opentimestamps.core.timestamp import Timestamp

import otsserver.rpc
from otsserver.calendar import Aggregator, Calendar, RecordCounts
from otsserver.tests.test_anchor_records import make_stamper


class RecordingAggregator:
    """Records (digest, counted) and answers with a serializable timestamp"""

    def __init__(self):
        self.calls = []

    def submit(self, digest, counted=True):
        self.calls.append((digest, counted))
        timestamp = Timestamp(digest)
        timestamp.attestations.add(PendingAttestation('http://127.0.0.1:14788'))
        return timestamp


def drive_post(path, body, lane_env=None):
    """Run do_POST(path) on a socketless handler; return (head, body, aggregator)

    lane_env None leaves OTSD_OPERATOR_LANE unset; a string sets it.
    """
    handler_cls = otsserver.rpc.RPCRequestHandler
    handler = handler_cls.__new__(handler_cls)
    aggregator = RecordingAggregator()
    handler.aggregator = aggregator
    handler.rfile = BytesIO(body)
    handler.wfile = BytesIO()
    handler.headers = {"Content-Length": str(len(body))}
    handler.path = path
    handler.command = "POST"
    handler.request_version = "HTTP/1.0"
    handler.requestline = "POST %s HTTP/1.0" % path
    handler.client_address = ("127.0.0.1", 0)

    with mock.patch.dict(os.environ):
        os.environ.pop('OTSD_OPERATOR_LANE', None)
        if lane_env is not None:
            os.environ['OTSD_OPERATOR_LANE'] = lane_env
        handler.do_POST()

    response = handler.wfile.getvalue()
    head, _, body_out = response.partition(b"\r\n\r\n")
    return head, body_out, aggregator


DIGEST = hashlib.sha256(b'the diary of the box').digest()


class Test_operator_lane_rpc(unittest.TestCase):
    def test_lane_off_is_an_ordinary_404_and_never_aggregates(self):
        head, body, aggregator = drive_post('/operator/digest', DIGEST)
        self.assertTrue(head.startswith(b"HTTP/1.0 404"), head[:40])
        self.assertIn(b"not found", body)
        self.assertEqual(aggregator.calls, [])

    def test_lane_must_be_exactly_1(self):
        head, _, aggregator = drive_post('/operator/digest', DIGEST, lane_env='true')
        self.assertTrue(head.startswith(b"HTTP/1.0 404"), head[:40])
        self.assertEqual(aggregator.calls, [])

    def test_lane_on_aggregates_uncounted_and_answers_the_timestamp(self):
        head, body, aggregator = drive_post('/operator/digest', DIGEST, lane_env='1')
        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertIn(b"application/octet-stream", head)
        self.assertEqual(aggregator.calls, [(DIGEST, False)])
        # The body is the same serialized pending timestamp /digest returns.
        self.assertIn(PendingAttestation.TAG, body)

    def test_client_digest_stays_counted_with_the_lane_on(self):
        head, _, aggregator = drive_post('/digest', DIGEST, lane_env='1')
        self.assertTrue(head.startswith(b"HTTP/1.0 200"), head[:40])
        self.assertEqual(aggregator.calls, [(DIGEST, True)])


def records_total(cal):
    return sum(c.kwargs['records'] for c in cal.submit.call_args_list)


def submit_all(aggregator, submissions):
    """Submit (msg, counted) pairs concurrently so they share rounds"""
    threads = [threading.Thread(target=aggregator.submit, args=(msg,),
                                kwargs={'counted': counted})
               for (msg, counted) in submissions]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()


class Test_operator_lane_counting(unittest.TestCase):
    def run_aggregator(self, submissions, cal=None):
        cal = cal if cal is not None else mock.Mock()
        exit_event = threading.Event()
        aggregator = Aggregator(cal, exit_event, commitment_interval=0.1)
        try:
            submit_all(aggregator, submissions)
        finally:
            exit_event.set()
            aggregator.thread.join(5)
        return cal

    def test_round_counts_only_client_leaves(self):
        cal = self.run_aggregator([
            (hashlib.sha256(b'client 1').digest(), True),
            (hashlib.sha256(b'client 2').digest(), True),
            (hashlib.sha256(b'client 3').digest(), True),
            (hashlib.sha256(b'operator diary').digest(), False),
        ])
        # However the rounds split, four leaves were aggregated and exactly
        # three of them are records.
        self.assertEqual(records_total(cal), 3)

    def test_operator_only_round_reports_records_zero(self):
        cal = self.run_aggregator([(hashlib.sha256(b'operator diary').digest(), False)])
        self.assertEqual(cal.submit.call_count, 1)
        self.assertEqual(cal.submit.call_args.kwargs['records'], 0)

    def test_dedupe_hit_never_changes_the_count(self):
        # First submission decides: operator then client -> 0 records ...
        msg = hashlib.sha256(b'same msg both lanes').digest()
        cal = mock.Mock()
        exit_event = threading.Event()
        aggregator = Aggregator(cal, exit_event, commitment_interval=0.1)
        try:
            first = aggregator.submit(msg, counted=False)
            second = aggregator.submit(msg, counted=True)
        finally:
            exit_event.set()
            aggregator.thread.join(5)
        self.assertIs(second, first)
        self.assertEqual(records_total(cal), 0)

        # ... and client then operator -> 1 record, never 2.
        cal = mock.Mock()
        exit_event = threading.Event()
        aggregator = Aggregator(cal, exit_event, commitment_interval=0.1)
        try:
            first = aggregator.submit(msg, counted=True)
            second = aggregator.submit(msg, counted=False)
        finally:
            exit_event.set()
            aggregator.thread.join(5)
        self.assertIs(second, first)
        self.assertEqual(records_total(cal), 1)


def pack_count(n):
    return struct.pack('>L', n)


class Test_known_zero_sentinel(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.cal_path = os.path.join(self.tmpdir.name, 'calendar')
        os.makedirs(self.cal_path)
        with open(os.path.join(self.cal_path, 'uri'), 'w') as fd:
            fd.write('http://127.0.0.1:14788\n')
        with open(os.path.join(self.cal_path, 'hmac-key'), 'wb') as fd:
            fd.write(b'\x01' * 32)
        self.counts_path = os.path.join(self.cal_path, 'journal.counts')

    def make_calendar(self):
        with mock.patch.dict(os.environ):
            os.environ['OTSD_ANCHOR_RECEIPTS'] = os.path.join(self.tmpdir.name, 'r.jsonl')
            return Calendar(self.cal_path)

    def test_reader_maps_sentinel_and_its_torn_prefixes_to_zero(self):
        with open(self.counts_path, 'wb') as fd:
            fd.write(pack_count(5) + pack_count(RecordCounts.KNOWN_ZERO)
                     + b'\xff\x00\x00\x00' + b'\xff\xff\x00\x00'
                     + b'\xff\xff\xff\x00' + pack_count(0)
                     + pack_count(RecordCounts.KNOWN_ZERO_THRESHOLD - 1))
        counts = RecordCounts(self.counts_path)
        self.assertEqual(counts.get(0), 5)           # legacy value untouched
        self.assertEqual(counts.get(1), 0)           # known zero
        self.assertEqual(counts.get(2), 0)           # torn 1-byte prefix
        self.assertEqual(counts.get(3), 0)           # torn 2-byte prefix
        self.assertEqual(counts.get(4), 0)           # torn 3-byte prefix
        self.assertIsNone(counts.get(5))             # hole: unknown
        self.assertEqual(counts.get(6), 0x7fffffff)  # largest real count

    def test_zero_writes_the_sentinel_and_none_writes_nothing(self):
        cal = self.make_calendar()
        with mock.patch('otsserver.calendar.time') as fake_time:
            fake_time.time.side_effect = [1000000000, 1000000001, 1000000002]
            cal.submit(Timestamp(hashlib.sha256(b'tree A').digest()), records=0)
            cal.submit(Timestamp(hashlib.sha256(b'tree B').digest()))
            cal.submit(Timestamp(hashlib.sha256(b'tree C').digest()), records=2)
        with open(self.counts_path, 'rb') as fd:
            data = fd.read()
        # idx 0 sentinel, idx 1 a hole (None wrote nothing), idx 2 a count.
        self.assertEqual(data, pack_count(RecordCounts.KNOWN_ZERO)
                         + pack_count(0) + pack_count(2))
        counts = RecordCounts(self.counts_path)
        self.assertEqual(counts.get(0), 0)
        self.assertIsNone(counts.get(1))
        self.assertEqual(counts.get(2), 2)

    def test_stamper_sums_a_known_zero_without_the_hole_warning(self):
        stamper = make_stamper(os.path.join(self.tmpdir.name, 'r.jsonl'))
        operator_only = Timestamp(hashlib.sha256(b'operator-only commitment').digest())
        client = Timestamp(hashlib.sha256(b'client commitment').digest())
        stamper.commitment_records = {operator_only.msg: 0, client.msg: 7}
        with self.assertNoLogs(level='WARNING'):
            records = stamper._Stamper__count_tree_records([operator_only, client])
        self.assertEqual(records, 7)


if __name__ == "__main__":
    unittest.main()
