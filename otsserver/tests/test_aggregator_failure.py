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

"""The aggregator must not die silently (D7, 2026-09-08 review).

Pre-fix, one exception in Aggregator.__loop — a journal fsync failing on a
full disk was the reproduction — killed the aggregator thread while the
HTTP server kept accepting digests: every later submit() waited on a
done_event nobody would ever set, each RPC thread pinned forever, and the
homepage still rendered "Best-block", so the gateway's /health said the
calendar was fine. Now a failed round is logged and exit_event is set so
the process leaves (the container restarts it), submit() waits at most
SUBMIT_TIMEOUT seconds and raises AggregatorUnavailable, and /digest
answers 503 instead of hanging.
"""

import hashlib
import threading
import time
import unittest
from io import BytesIO
from unittest import mock

import otsserver.rpc
from otsserver.calendar import Aggregator, AggregatorUnavailable


class RaisingCalendar:
    calls = 0

    def submit(self, commitment, records=None):
        RaisingCalendar.calls += 1
        raise OSError(28, 'No space left on device')


class Test_aggregator_failure(unittest.TestCase):
    def test_failed_round_logs_and_sets_exit_event(self):
        exit_event = threading.Event()
        aggregator = Aggregator(RaisingCalendar(), exit_event, commitment_interval=0.1)
        try:
            with self.assertLogs(level='ERROR') as captured:
                with self.assertRaises(AggregatorUnavailable):
                    aggregator.submit(hashlib.sha256(b'first').digest())
            self.assertTrue(exit_event.is_set(), 'a dead aggregator must stop the process')
            self.assertTrue(any('aggregator' in line.lower() and 'No space left' in line
                                for line in captured.output), captured.output)
        finally:
            exit_event.set()
            aggregator.thread.join(5)
        self.assertFalse(aggregator.thread.is_alive())

    def test_submit_is_bounded_when_the_loop_is_gone(self):
        exit_event = threading.Event()
        aggregator = Aggregator(RaisingCalendar(), exit_event, commitment_interval=0.1)
        try:
            with self.assertRaises(AggregatorUnavailable):
                aggregator.submit(hashlib.sha256(b'first').digest())
            aggregator.thread.join(5)
            # The loop is gone; a second caller must not wait forever.
            aggregator.submit_timeout = 0.5
            t0 = time.time()
            with self.assertRaises(AggregatorUnavailable):
                aggregator.submit(hashlib.sha256(b'second').digest())
            self.assertLess(time.time() - t0, 5)
        finally:
            exit_event.set()

    def test_rpc_answers_503_not_a_hang(self):
        class DeadAggregator:
            def submit(self, digest, counted=True):
                raise AggregatorUnavailable('aggregator loop has stopped')
        handler_cls = otsserver.rpc.RPCRequestHandler
        handler = handler_cls.__new__(handler_cls)
        handler.aggregator = DeadAggregator()
        body = hashlib.sha256(b'x').digest()
        handler.rfile = BytesIO(body)
        handler.wfile = BytesIO()
        handler.headers = {"Content-Length": str(len(body))}
        handler.path = "/digest"
        handler.command = "POST"
        handler.request_version = "HTTP/1.0"
        handler.requestline = "POST /digest HTTP/1.0"
        handler.client_address = ("127.0.0.1", 0)
        handler.do_POST()
        head, _, out = handler.wfile.getvalue().partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.0 503"), head[:40])
        self.assertIn(b"aggregator unavailable", out)


if __name__ == "__main__":
    unittest.main()
